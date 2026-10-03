"""The service layer: everything the UI does goes through here.

Each call takes an Actor, checks permissions, validates input, writes through the Store, and appends to
the audit log. The UI never touches the Store directly.
"""
import hashlib
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from p2.pipeline.runner import BuildResult, DataQualityError, StepResult, end_of
from p2.services import errors
from p2.services.catalog import CatalogMixin, seed_rows
from p2.services.runs import RunManager
from p2.services.permissions import Actor, require_admin, require_create_experiment, require_edit_experiment
from p2.store import base as store_errors
from p2.stats.plan import MetricPlan
from p2.stats.tests import analyze_metric
from p2.store.base import Store
from p2.warehouse.sources import SourceDef, SourceRegistry

STATUS_ORDER = ["Draft", "Designed", "Running", "Analyzed", "Decided"]
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")  # lowercase letters, digits, hyphens: maps onto warehouse table names
ROLE_ORDER = {"primary": 0, "guardrail": 1, "secondary": 2, "filter": 3, "dimension": 4}
MAX_METRICS, MAX_DIMENSIONS, MAX_FILTERS = 30, 10, 5  # keeps one experiment build manageable

# Simulated team for the portfolio project: one owner per product, an admin, and a read-only viewer.
SEED_MEMBERS = [
    ("priya", "Priya (Checkout owner)", "metric_owner", "checkout"),
    ("marcus", "Marcus (Email owner)", "metric_owner", "email"),
    ("sofia", "Sofia (Onboarding owner)", "metric_owner", "onboarding"),
    ("admin", "Platform admin", "admin", None),
    ("viewer", "Viewer (read only)", "viewer", None),
]
SEED_PRODUCTS = [("checkout", "Checkout", "priya"), ("email", "Email", "marcus"), ("onboarding", "Onboarding", "sofia")]
DECISIONS = ("ship", "no ship", "iterate", "inconclusive")
MAX_RUNTIME_DAYS = 365


def _source_row(src: SourceDef, now: datetime) -> dict:
    return {
        "source_id": src.source_id, "product_id": src.product_id, "raw_table": src.source_id,
        "staging_view": src.staging_name, "dedupe_key": list(src.dedupe_key), "dedupe_order": src.dedupe_order,
        "time_column": src.time_column, "user_column": src.user_column, "description": src.description,
        "owner": "admin", "status": "active",
        "schema": [{"name": c.name, "type": c.type, "nullable": c.nullable, "description": c.description} for c in src.columns],
        "created_at": now, "updated_at": now,
    }


class Platform(CatalogMixin):
    def __init__(self, store: Store, sources: SourceRegistry, clock=None, checker=None, runner=None, max_runs: int = 4,
                 inline_runs: bool = False, retry_pauses: tuple[float, ...] = (5.0, 15.0, 45.0)):
        """`checker` runs a catalog item's automated checks; `runner` builds experiment data. Both need a warehouse
        and are optional, so the service layer also works (and is tested) with neither."""
        self.store, self.sources = store, sources
        self.clock = clock or store.clock
        self.checker, self.runner = checker, runner
        self.runs = RunManager(self._do_run, max_concurrent=max_runs, retry_pauses=retry_pauses, inline=inline_runs)

    # ---- plumbing --------------------------------------------------------------------------
    def _audit(self, actor_id: str, action: str, entity_type: str, entity_id: str, detail: dict | None = None) -> None:
        self.store.insert("audit_log", {"audit_id": uuid.uuid4().hex, "ts": self.clock(), "actor": actor_id,
                                        "action": action, "entity_type": entity_type, "entity_id": entity_id,
                                        "detail": detail})

    @staticmethod
    def _translate(e: Exception) -> Exception:
        if isinstance(e, store_errors.NotFound):
            return errors.NotFound(str(e))
        if isinstance(e, store_errors.Conflict):
            return errors.Conflict(str(e))
        if isinstance(e, store_errors.AlreadyExists):
            return errors.InvalidInput(str(e))
        return e

    # ---- team, products, sources -----------------------------------------------------------
    def get_actor(self, user_id: str) -> Actor:
        row = self.store.get("team_members", {"user_id": user_id})
        if row is None:
            raise errors.NotFound(f"unknown team member: {user_id}")
        if not row["active"]:
            raise errors.PermissionDenied(f"{row['name']} is deactivated")
        return Actor(row["user_id"], row["name"], row["role"], row["product_id"])

    def list_team(self) -> list[dict]:
        return self.store.select("team_members", order_by=["user_id"])

    def list_products(self) -> list[dict]:
        return self.store.select("products", order_by=["product_id"])

    def list_sources(self, product_id: str | None = None) -> list[dict]:
        rows = self.store.select("data_sources", order_by=["source_id"])
        return [r for r in rows if product_id is None or r["product_id"] in (product_id, "shared")]

    def register_source(self, actor: Actor, src: SourceDef) -> None:
        require_admin(actor)
        try:
            self.store.insert("data_sources", _source_row(src, self.clock()))
        except store_errors.AlreadyExists:
            raise errors.InvalidInput(f"source {src.source_id} is already registered") from None
        self._audit(actor.user_id, "source.register", "data_source", src.source_id)

    def bootstrap(self) -> dict[str, int]:
        """Create the simulated team, products, and registered sources if absent. Safe to repeat."""
        now, created = self.clock(), {}

        def seed(table: str, rows: list[dict]) -> None:
            added = self.store.insert_missing(table, rows)  # atomic: concurrent bootstraps cannot both insert
            if added:
                self.store.insert("audit_log", {"audit_id": uuid.uuid4().hex, "ts": now, "actor": "system", "action": "seed",
                                                "entity_type": table, "entity_id": table, "detail": {"rows": added}})
            created[table] = added

        seed("team_members", [{"user_id": u, "name": n, "role": r, "product_id": p, "active": True,
                               "created_at": now, "updated_at": now} for u, n, r, p in SEED_MEMBERS])
        seed("products", [{"product_id": p, "name": n, "owner_user_id": o, "created_at": now, "updated_at": now}
                          for p, n, o in SEED_PRODUCTS])
        seed("data_sources", [_source_row(s, now) for s in self.sources])
        seed("catalog_items", seed_rows(now))
        return created

    # ---- experiments -----------------------------------------------------------------------
    def _validate_header(self, name: str, launch_date: date, runtime_days: int, end_date: date | None = None) -> None:
        if not name.strip():
            raise errors.InvalidInput("a name is required")
        if not isinstance(launch_date, date) or isinstance(launch_date, datetime):
            raise errors.InvalidInput("the launch date is required")
        if not isinstance(runtime_days, int) or isinstance(runtime_days, bool) or not 1 <= runtime_days <= MAX_RUNTIME_DAYS:
            raise errors.InvalidInput(f"runtime must be a whole number of days between 1 and {MAX_RUNTIME_DAYS}")
        if end_date is not None:
            if not isinstance(end_date, date) or isinstance(end_date, datetime):
                raise errors.InvalidInput("the end date must be a date")
            if end_date < launch_date:
                raise errors.InvalidInput(f"the end date ({end_date}) cannot be before the launch date ({launch_date})")
            if (end_date - launch_date).days >= MAX_RUNTIME_DAYS:
                raise errors.InvalidInput(f"the end date cannot be more than {MAX_RUNTIME_DAYS} days after the launch")

    def create_experiment(self, actor: Actor, experiment_id: str, product_id: str, name: str, hypothesis: str,
                          launch_date: date, runtime_days: int, end_date: date | None = None, owner_user_id: str | None = None) -> dict:
        """`owner_user_id` is the team member who owns the experiment (the actor when not given). `runtime_days` is the planned runtime from the data scientist's power calculation. `end_date`, if given, is the real last day
        (for example when the experiment will keep running); without it the last day is launch plus runtime minus one."""
        if not ID_PATTERN.fullmatch(experiment_id):
            raise errors.InvalidInput("experiment id must be lowercase letters, digits and hyphens (max 41 characters)")
        if self.store.get("products", {"product_id": product_id}) is None:
            raise errors.InvalidInput(f"unknown product: {product_id}")
        require_create_experiment(actor, product_id)
        self._validate_header(name, launch_date, runtime_days, end_date)
        owner = self._check_owner(owner_user_id or actor.user_id)
        now = self.clock()
        row = {"experiment_id": experiment_id, "product_id": product_id, "owner_user_id": owner, "name": name.strip(),
               "hypothesis": (hypothesis or "").strip(), "status": "Draft", "launch_date": launch_date,
               "runtime_days": runtime_days, "end_date": end_date, "created_at": now, "updated_at": now}
        try:
            self.store.insert("experiments", row)
        except store_errors.AlreadyExists:
            raise errors.InvalidInput(f"experiment {experiment_id} already exists") from None
        self._audit(actor.user_id, "experiment.create", "experiment", experiment_id, {"product": product_id, "owner": owner})
        return row

    def _check_owner(self, user_id: str) -> str:
        member = self.store.get("team_members", {"user_id": user_id})
        if member is None or not member["active"]:
            raise errors.InvalidInput(f"unknown or inactive team member: {user_id}")
        return user_id

    def get_experiment(self, experiment_id: str) -> dict:
        row = self.store.get("experiments", {"experiment_id": experiment_id})
        if row is None:
            raise errors.NotFound(f"unknown experiment: {experiment_id}")
        return row

    def list_experiments(self, product_id: str | None = None, owner_user_id: str | None = None,
                         status: str | None = None) -> list[dict]:
        where = {k: v for k, v in (("product_id", product_id), ("owner_user_id", owner_user_id), ("status", status)) if v}
        return self.store.select("experiments", where, order_by=["created_at DESC", "experiment_id"])

    def list_portfolio(self, product_id: str | None = None, owner_user_id: str | None = None, status: str | None = None,
                       search: str | None = None) -> list[dict]:
        """Every experiment with a one-line design summary, for the portfolio view. Two store reads, however many experiments."""
        rows = self.list_experiments(product_id, owner_user_id, status)
        if search:
            q = search.lower()
            rows = [r for r in rows if q in r["experiment_id"] or q in r["name"].lower() or q in r["hypothesis"].lower()]
        by_exp: dict[str, list[dict]] = {}
        for it in self.store.select("experiment_items"):
            by_exp.setdefault(it["experiment_id"], []).append(it)
        return [{**r, **self._summarize(r, by_exp.get(r["experiment_id"], []))} for r in rows]

    def experiment_catalog(self) -> list[dict]:
        """Every experiment, newest first, with its one-line design summary and the verdict on its primary metric. The verdict is
        there only when the latest run was the final analysis; otherwise it is None (still live, or not run yet)."""
        rows = self.list_portfolio()
        latest: dict[str, dict] = {}
        for r in self.store.select("results", {"role": "primary"}):
            if r["experiment_id"] not in latest or r["run_at"] > latest[r["experiment_id"]]["run_at"]:
                latest[r["experiment_id"]] = r
        for row in rows:
            last = latest.get(row["experiment_id"])
            row["verdict"] = last["verdict"] if last and (last.get("kind") or "final") == "final" else None
        return rows

    @staticmethod
    def _summarize(exp: dict, items: list[dict]) -> dict:
        return {
            "primary_metric": next((i["item_id"] for i in items if i["role"] == "primary"), None),
            "n_items": len(items),
            "end_date": end_of(exp) if exp.get("launch_date") and exp.get("runtime_days") else None,
        }

    def experiment_summary(self, experiment_id: str) -> dict:
        exp = self.get_experiment(experiment_id)
        return self._summarize(exp, self.store.select("experiment_items", {"experiment_id": experiment_id}))

    def experiment_history(self, experiment_id: str) -> list[dict]:
        """Who did what to an experiment, newest first. Open to every team member (the full audit log is admin only)."""
        self.get_experiment(experiment_id)
        return self.store.select("audit_log", {"entity_id": experiment_id}, order_by=["ts DESC"])

    def update_experiment(self, actor: Actor, experiment_id: str, name: str, hypothesis: str, launch_date: date,
                          runtime_days: int, expected_updated_at: datetime | None = None, end_date: date | None = None,
                          owner_user_id: str | None = None, product_id: str | None = None) -> dict:
        exp = self.get_experiment(experiment_id)
        require_edit_experiment(actor, exp["owner_user_id"])
        self._validate_header(name, launch_date, runtime_days, end_date)
        changes = {"name": name.strip(), "hypothesis": (hypothesis or "").strip(), "launch_date": launch_date,
                   "runtime_days": runtime_days, "end_date": end_date}
        if owner_user_id:
            changes["owner_user_id"] = self._check_owner(owner_user_id)
        if product_id:
            if self.store.get("products", {"product_id": product_id}) is None:
                raise errors.InvalidInput(f"unknown product: {product_id}")
            changes["product_id"] = product_id
        try:
            row = self.store.update("experiments", {"experiment_id": experiment_id}, changes, expected_updated_at)
        except (store_errors.NotFound, store_errors.Conflict) as e:
            raise self._translate(e) from None
        self._audit(actor.user_id, "experiment.update", "experiment", experiment_id)
        return row

    def _move_status(self, experiment_id: str, status: str) -> dict:
        exp = self.get_experiment(experiment_id)
        if status not in STATUS_ORDER:
            raise errors.InvalidInput(f"unknown status: {status}")
        if STATUS_ORDER.index(status) < STATUS_ORDER.index(exp["status"]):
            raise errors.InvalidInput(f"cannot move {experiment_id} from {exp['status']} back to {status}")
        return self.store.update("experiments", {"experiment_id": experiment_id}, {"status": status})

    def set_status(self, actor: Actor, experiment_id: str, status: str) -> dict:
        exp = self.get_experiment(experiment_id)
        require_edit_experiment(actor, exp["owner_user_id"])
        row = self._move_status(experiment_id, status)
        self._audit(actor.user_id, "experiment.status", "experiment", experiment_id, {"from": exp["status"], "to": status})
        return row

    # ---- design ----------------------------------------------------------------------------
    def save_design(self, actor: Actor, experiment_id: str, primary: MetricPlan, guardrails: list[MetricPlan],
                    secondary_ids: list[str], dimension_ids: list[str] | tuple = (), filter_ids: list[str] | tuple = ()) -> None:
        """Validate the picks and store the plan: one row per metric, filter and dimension. The numbers are the data
        scientist's own; the tool records and follows them."""
        exp = self.get_experiment(experiment_id)
        require_edit_experiment(actor, exp["owner_user_id"])
        registry = self.metric_registry()
        plans = [primary, *guardrails]
        attr_rows = []
        for kind, ids, cap in (("filter", filter_ids, MAX_FILTERS), ("dimension", dimension_ids, MAX_DIMENSIONS)):
            if len(set(ids)) != len(ids):
                raise errors.InvalidInput(f"a {kind} can only be picked once")
            if len(ids) > cap:
                raise errors.InvalidInput(f"at most {cap} {kind}s per experiment")
            available = {r["item_id"]: r for r in self.certified_items(kind, exp["product_id"])}
            for item_id in ids:
                if item_id not in available:
                    raise errors.InvalidInput(f"{item_id} is not a certified {kind} available to {exp['product_id']} experiments")
                attr_rows.append({"experiment_id": experiment_id, "item_id": item_id, "item_version": available[item_id]["version"],
                                  "kind": kind, "role": kind})
        if len(plans) + len(secondary_ids) > MAX_METRICS:
            raise errors.InvalidInput(f"at most {MAX_METRICS} metrics per experiment")
        try:
            registry.validate_selection(primary.metric.metric_id, [g.metric.metric_id for g in guardrails], secondary_ids)
            for pl in plans:  # the definition in the plan must be the catalog's, pinned as-is
                if registry.get(pl.metric.metric_id, pl.metric.version) != pl.metric:
                    raise ValueError(f"{pl.metric.metric_id} does not match the certified catalog definition")
            if primary.role != "primary" or any(g.role != "guardrail" for g in guardrails):
                raise ValueError("roles in the plan do not match the selection")
        except (ValueError, KeyError) as e:
            raise errors.InvalidInput(str(e.args[0]) if isinstance(e, KeyError) else str(e)) from None
        rows = [{"experiment_id": experiment_id, "item_id": pl.metric.metric_id, "item_version": pl.metric.version,
                 "kind": "metric", "role": pl.role, "baseline": pl.baseline, "std": pl.std, "effect": pl.effect,
                 "effect_kind": pl.effect_kind, "alpha": pl.alpha, "power": pl.power, "sidedness": pl.sidedness} for pl in plans]
        rows += [{"experiment_id": experiment_id, "item_id": m, "item_version": registry.get(m).version,
                  "kind": "metric", "role": "secondary"} for m in secondary_ids]
        rows += attr_rows
        old = self.store.select("experiment_items", {"experiment_id": experiment_id})
        self.store.delete("experiment_items", {"experiment_id": experiment_id})
        try:
            self.store.insert_many("experiment_items", rows)
        except Exception:
            if old:
                self.store.insert_many("experiment_items", old)  # put the previous plan back
            raise
        if exp["status"] == "Draft":     # a plan can be changed at any time; the status only follows the first save and the runs
            self._move_status(experiment_id, "Designed")
        self._audit(actor.user_id, "experiment.design", "experiment", experiment_id,
                    {"primary": primary.metric.metric_id, "guardrails": [g.metric.metric_id for g in guardrails],
                     "secondary": list(secondary_ids), "filters": list(filter_ids), "dimensions": list(dimension_ids)})

    def get_design(self, experiment_id: str) -> list[dict]:
        rows = self.store.select("experiment_items", {"experiment_id": experiment_id})
        return sorted(rows, key=lambda r: (ROLE_ORDER[r["role"]], r["item_id"]))

    # ---- running the analysis (needs a warehouse runner) ---------------------------------------
    def _need_runner(self):
        if self.runner is None:
            raise errors.InvalidInput("no warehouse is connected, so data cannot be built")
        return self.runner

    def assignment_check(self, experiment_id: str) -> dict:
        """What the experiment tool's log holds for this id (users, arms, first and last day), or {} with no warehouse."""
        return self.runner.assignment_summary(experiment_id) if self.runner is not None else {}

    def refresh_monitor(self, actor: Actor, experiment_id: str, as_of: datetime | None = None) -> BuildResult:
        """Live monitoring while the experiment runs: build the data through yesterday and save descriptive numbers only.

        There are no p-values, intervals or verdicts, because a fixed-horizon test must not be read before the experiment
        ends. Status becomes Running."""
        return self._run(actor, experiment_id, as_of, final=False)

    def run_analysis(self, actor: Actor, experiment_id: str, as_of: datetime | None = None) -> BuildResult:
        """The final analysis, allowed once the last day is over: full tests and verdicts, status Analyzed."""
        return self._run(actor, experiment_id, as_of, final=True)

    # ---- runs in the background --------------------------------------------------------------------
    def _do_run(self, actor: Actor, experiment_id: str, final: bool) -> BuildResult:
        return self.run_analysis(actor, experiment_id) if final else self.refresh_monitor(actor, experiment_id)

    def start_run(self, actor: Actor, experiment_id: str, final: bool | None = None) -> dict:
        """Put a Run in the queue and return at once with its status. `final` None picks the right kind from the dates: the final analysis
        after the experiment's last day, a live refresh before it. The obvious mistakes (no plan, not launched, final too early) are
        refused here, immediately, instead of after waiting in the queue."""
        self._need_runner()
        exp = self.get_experiment(experiment_id)
        require_edit_experiment(actor, exp["owner_user_id"])
        if exp["status"] not in ("Designed", "Running", "Analyzed", "Decided"):
            raise errors.InvalidInput(f"{experiment_id} is {exp['status']}; save its plan before running it")
        if not any(r["kind"] == "metric" for r in self.get_design(experiment_id)):
            raise errors.InvalidInput(f"{experiment_id} has no metrics in its plan")
        today, end = datetime.now(timezone.utc).date(), end_of(exp)
        final = end < today if final is None else final
        if exp["launch_date"] > today:
            raise errors.InvalidInput(f"{experiment_id}: the experiment has not launched yet (launch date {exp['launch_date']})")
        if final and end >= today:
            raise errors.InvalidInput(f"{experiment_id}: the experiment is still running until {end}; the final analysis can run after that day")
        return self.runs.submit(actor, experiment_id, final).view()

    def run_status(self, experiment_id: str) -> dict | None:
        """The queued, running or latest finished Run of an experiment (None if there is none)."""
        return self.runs.status(experiment_id)

    @staticmethod
    def _plan_stamp(exp: dict, design: list[dict]) -> str:
        """A short fingerprint of everything that decides what a Run calculates: the dates and the whole plan."""
        parts = [str(exp["launch_date"]), str(end_of(exp))] + sorted(
            "|".join(str(r.get(k)) for k in ("kind", "item_id", "item_version", "role", "baseline", "std", "effect", "effect_kind", "alpha", "power", "sidedness"))
            for r in design)
        return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:12]

    def plan_changes_since_run(self, experiment_id: str) -> list[str]:
        """What was edited after the numbers on the results page were calculated, in plain words (empty when nothing, or when the last
        successful Run predates this check)."""
        exp = self.get_experiment(experiment_id)
        last = next((j for j in self.list_jobs(experiment_id) if j["status"] == "succeeded" and j["detail"]), None)
        if last is None:
            return []
        then, design = last["detail"], self.get_design(experiment_id)
        if not then.get("plan"):            # a Run from before plans were fingerprinted: a final analysis still tells us its last day
            if then.get("final") and then.get("through_date") and then["through_date"] != end_of(exp).isoformat():
                return [f"the experiment now ends {end_of(exp)} (these numbers run through {then['through_date']})"]
            return []
        changes = []
        if then.get("launch") != exp["launch_date"].isoformat():
            changes.append(f"the launch date is now {exp['launch_date']} (it was {then.get('launch')})")
        if then.get("end") != end_of(exp).isoformat():
            changes.append(f"the end date is now {end_of(exp)} (it was {then.get('end')})")
        if not changes and then["plan"] != self._plan_stamp(exp, design):
            changes.append("the metrics, segments, filters or statistical numbers in the plan were changed")
        return changes

    def recover_interrupted_runs(self, older_than_minutes: int = 10) -> int:
        """Mark Runs left "running" by a server that stopped as failed, so they do not look active for ever. Returns how many."""
        cutoff = self.clock() - timedelta(minutes=older_than_minutes)
        stuck = [j for j in self.store.select("job_runs", {"status": "running"}) if j["started_at"] < cutoff]
        for j in stuck:
            self.store.update("job_runs", {"job_id": j["job_id"]}, {"status": "failed", "finished_at": self.clock(),
                                                                    "error": "interrupted when the server stopped"})
        return len(stuck)

    def _run(self, actor: Actor, experiment_id: str, as_of: datetime | None, final: bool) -> BuildResult:
        runner = self._need_runner()
        exp = self.get_experiment(experiment_id)
        require_edit_experiment(actor, exp["owner_user_id"])
        allowed = ("Designed", "Running", "Analyzed", "Decided")
        if exp["status"] not in allowed:
            raise errors.InvalidInput(f"{experiment_id} is {exp['status']}; save its plan before running it")
        design = self.get_design(experiment_id)
        items: dict[str, list[dict]] = {"metric": [], "dimension": [], "filter": []}
        for r in design:
            items[r["kind"]].append(self.get_item(r["item_id"], r["item_version"]))
        if not items["metric"]:
            raise errors.InvalidInput(f"{experiment_id} has no metrics in its plan")
        end = end_of(exp)
        today = (as_of or datetime.now(timezone.utc)).date()
        through = end if final else min(end, today - timedelta(days=1))   # monitoring covers complete days only
        job_id = uuid.uuid4().hex
        self.store.insert("job_runs", {"job_id": job_id, "experiment_id": experiment_id, "type": "analysis" if final else "monitor",
                                       "status": "running", "started_at": self.clock(), "finished_at": None, "error": None, "detail": None})
        result, error, steps, rows, series, seg_rows = None, None, [], [], [], []
        try:
            result = runner.build(experiment_id, exp["product_id"], items["metric"], items["dimension"], items["filter"],
                                  exp["launch_date"], through, as_of, final)
            steps = list(result.steps)
            ids = [m["item_id"] for m in items["metric"]]
            summary = runner.summary_stats(result.final_table, ids)
            run_at = self.clock()
            rows = (self._outcomes(experiment_id, design, summary, run_at, through) if final
                    else self._interim(experiment_id, design, summary, run_at, through))
            steps.append(StepResult("stats", None, "ok", len(rows), 0.0))
            series = runner.daily_series(experiment_id, items["metric"], exp["launch_date"], through)
            steps.append(StepResult("daily", None, "ok", len(series), 0.0))
            dim_ids, flt_ids = [d["item_id"] for d in items["dimension"]], [f["item_id"] for f in items["filter"]]
            if dim_ids or flt_ids:
                seg = runner.segment_stats(result.final_table, dim_ids, flt_ids, ids)
                seg_rows = self._segments(experiment_id, design, seg, run_at, through, final)
                steps.append(StepResult("segments", None, "ok", len(seg_rows), 0.0))
        except Exception as e:                           # any failure ends the job cleanly instead of leaving it "running" for ever
            expected = isinstance(e, (DataQualityError, ValueError))
            error = str(e) if expected else f"{type(e).__name__}: {e}"
            temporary = errors.is_transient(e if not expected else error)
            steps = list(getattr(runner, "last_steps", steps))
            if result is not None:
                steps.append(StepResult("stats", None, "failed", None, 0.0, error))
        if steps:
            self.store.insert_many("job_steps", [{"job_id": job_id, "step": s.step, "table_name": s.table, "status": s.status,
                                                  "row_count": s.rows, "duration_s": s.seconds} for s in steps])
        self.store.update("job_runs", {"job_id": job_id}, {
            "status": "failed" if error else "succeeded", "finished_at": self.clock(), "error": error,
            "detail": ({"cohort_users": result.cohort_users, "audience_users": result.audience_users, "n_control": result.n_control,
                        "n_variant": result.n_variant, "through_date": through.isoformat(), "final": final,
                       "launch": exp["launch_date"].isoformat(), "end": end.isoformat(), "plan": self._plan_stamp(exp, design)}
                      if result and not error else None)})
        if error:
            self._audit(actor.user_id, "pipeline.failed", "experiment", experiment_id, {"job_id": job_id, "error": error})
            if temporary:
                raise errors.TransientError(error)
            raise errors.InvalidInput(f"Data-quality gate failed: {error}" if expected else f"The run failed: {error}")
        self.store.insert_many("results", rows)
        self.store.delete("daily_stats", {"experiment_id": experiment_id})   # the chart always matches the latest run
        self.store.delete("segment_results", {"experiment_id": experiment_id})
        if seg_rows:
            self.store.insert_many("segment_results", seg_rows)
        self.store.insert_many("daily_stats", [{"experiment_id": experiment_id, "run_at": run_at, **s} for s in series])
        if exp["status"] != "Decided":    # the status follows the latest run; a recorded decision stays
            self.store.update("experiments", {"experiment_id": experiment_id}, {"status": "Analyzed" if final else "Running"})
        self._audit(actor.user_id, "analysis.run" if final else "monitor.refresh", "experiment", experiment_id,
                    {"job_id": job_id, "audience_users": result.audience_users, "through": through.isoformat()})
        return result

    def _interim(self, experiment_id: str, design: list[dict], summary: dict, run_at: datetime, through: date) -> list[dict]:
        """Descriptive numbers for monitoring: counts, means, difference and lift. Deliberately no tests or intervals."""
        rows = []
        for d in design:
            if d["kind"] != "metric":
                continue
            arms = summary[d["item_id"]]
            if "control" not in arms or "variant" not in arms:
                raise ValueError(f"{d['item_id']}: the built table has no users in one arm")
            c, v = arms["control"], arms["variant"]
            rows.append({"experiment_id": experiment_id, "item_id": d["item_id"], "run_at": run_at, "role": d["role"],
                         "mean_control": c.mean, "mean_variant": v.mean, "difference": v.mean - c.mean,
                         "relative_lift": (v.mean - c.mean) / c.mean if c.mean else None, "n_control": c.n, "n_variant": v.n,
                         "kind": "interim", "through_date": through, "params": {"interim": True}})
        return rows

    def _segments(self, experiment_id: str, design: list[dict], seg: dict, run_at: datetime, through: date, final: bool) -> list[dict]:
        """One row per slice and metric. A slice is a dimension value, a filter (users who pass it) or both together. Exploratory: every
        metric gets the plain two-sided test at 0.05 (no plan numbers, no correction for the number of slices). A slice needs both arms,
        and two users in each for the final test."""
        registry = self.metric_registry()
        plan = {d["item_id"]: d for d in design if d["kind"] == "metric"}
        rows = []
        for (dim, flt), segments in seg.items():
            for segment, metrics in segments.items():
                for item_id, arms in metrics.items():
                    if "control" not in arms or "variant" not in arms:
                        continue
                    c, v = arms["control"], arms["variant"]
                    if final and (c.n < 2 or v.n < 2):
                        continue
                    base = {"experiment_id": experiment_id, "item_id": item_id, "dimension_id": dim, "segment": str(segment), "filter_id": flt,
                            "run_at": run_at, "kind": "final" if final else "interim", "role": plan[item_id]["role"], "mean_control": c.mean,
                            "mean_variant": v.mean, "n_control": c.n, "n_variant": v.n, "through_date": through,
                            "difference": v.mean - c.mean, "relative_lift": (v.mean - c.mean) / c.mean if c.mean else None}
                    if final:
                        out = analyze_metric(registry.get(item_id, plan[item_id]["item_version"]), "secondary", c, v, None)
                        base.update({"difference": out.difference, "relative_lift": out.relative_lift, "ci_low": out.ci_low,
                                     "ci_high": out.ci_high, "ci_level": out.ci_level, "p_value": out.p_value, "verdict": out.verdict})
                    rows.append(base)
        return rows

    def segment_results(self, experiment_id: str) -> list[dict]:
        """Numbers per slice from the latest run, ordered by dimension, segment, filter and the metric's role in the plan."""
        rows = self.store.select("segment_results", {"experiment_id": experiment_id})
        return sorted(rows, key=lambda r: (r["dimension_id"] or "", r["segment"], r["filter_id"] or "", ROLE_ORDER[r["role"]], r["item_id"]))

    def _outcomes(self, experiment_id: str, design: list[dict], summary: dict, run_at: datetime, through: date) -> list[dict]:
        """Run the right test for every metric in its role and shape the rows for the results table."""
        registry = self.metric_registry()
        rows = []
        for d in design:
            if d["kind"] != "metric":
                continue
            metric = registry.get(d["item_id"], d["item_version"])
            arms = summary[d["item_id"]]
            if "control" not in arms or "variant" not in arms:
                raise ValueError(f"{d['item_id']}: the built table has no users in one arm")
            plan = None
            if d["role"] in ("primary", "guardrail"):
                plan = MetricPlan(metric, d["role"], d["baseline"], d["effect"], d["effect_kind"], d["alpha"], d["power"],
                                  d["sidedness"], d["std"])
            out = analyze_metric(metric, d["role"], arms["control"], arms["variant"], plan)
            params = ({"alpha": plan.alpha, "sidedness": plan.sidedness, "effect": plan.effect, "effect_kind": plan.effect_kind,
                       "baseline": plan.baseline, "planned_power": plan.power, "effect_abs": plan.effect_abs}
                      if plan else {"alpha": 0.05, "sidedness": "two-sided", "exploratory": True})
            rows.append({"experiment_id": experiment_id, "item_id": d["item_id"], "run_at": run_at, "role": d["role"],
                         "mean_control": arms["control"].mean, "mean_variant": arms["variant"].mean, "difference": out.difference,
                         "ci_low": out.ci_low, "ci_high": out.ci_high, "p_value": out.p_value, "verdict": out.verdict,
                         "achieved_power": out.achieved_power, "n_control": arms["control"].n, "n_variant": arms["variant"].n,
                         "relative_lift": out.relative_lift, "ci_level": out.ci_level, "kind": "final", "through_date": through,
                         "params": params})
        return rows

    def daily_series(self, experiment_id: str) -> list[dict]:
        """Cumulative mean and users per metric, day and arm from the latest monitoring refresh or final analysis."""
        return self.store.select("daily_stats", {"experiment_id": experiment_id}, order_by=["item_id", "day", "arm"])

    def latest_results(self, experiment_id: str, kind: str = "final") -> list[dict]:
        """The rows of the most recent run of one kind ('final' or 'interim'), primary first. Older rows without a kind are final."""
        rows = [r for r in self.store.select("results", {"experiment_id": experiment_id}, order_by=["run_at DESC"])
                if (r.get("kind") or "final") == kind]
        if not rows:
            return []
        latest = rows[0]["run_at"]
        return sorted((r for r in rows if r["run_at"] == latest), key=lambda r: (ROLE_ORDER[r["role"]], r["item_id"]))

    def record_decision(self, actor: Actor, experiment_id: str, decision: str, notes: str) -> dict:
        """The call the team made once the results were in; moves the experiment to Decided."""
        exp = self.get_experiment(experiment_id)
        require_edit_experiment(actor, exp["owner_user_id"])
        if exp["status"] != "Analyzed":
            raise errors.InvalidInput(f"{experiment_id} is {exp['status']}; a decision can be recorded once it is Analyzed")
        if decision not in DECISIONS:
            raise errors.InvalidInput(f"decision must be one of {list(DECISIONS)}")
        if not (notes or "").strip():
            raise errors.InvalidInput("say why, in a sentence or two")
        now = self.clock()
        row = self.store.update("experiments", {"experiment_id": experiment_id},
                                {"decision": decision, "decision_notes": notes.strip(), "decided_by": actor.user_id,
                                 "decided_at": now, "status": "Decided"})
        self._audit(actor.user_id, "experiment.decision", "experiment", experiment_id, {"decision": decision})
        return row

    def simulate_data(self, actor: Actor, experiment_id: str, multipliers: dict[str, float], n_users: int = 5000,
                      seed: int = 0) -> dict:
        """Dev only: land simulated assignments and product activity for this experiment, with a planted effect.

        Plays the company's logging systems. Assignments are spread over the first runtime_days minus 6 days, so every
        user's first seven days of activity fall inside the runtime and the planted effect is exact."""
        runner = self._need_runner()
        exp = self.get_experiment(experiment_id)
        require_edit_experiment(actor, exp["owner_user_id"])
        if exp["status"] not in ("Designed", "Running", "Analyzed", "Decided"):
            raise errors.InvalidInput(f"{experiment_id} is {exp['status']}; save its plan before simulating data")
        if exp["runtime_days"] < 7:
            raise errors.InvalidInput("simulated users are active for 7 days after assignment; use a runtime of at least 7 days")
        if not 100 <= n_users <= 100_000:
            raise errors.InvalidInput("simulate between 100 and 100,000 users")
        from p2.simulator.service import simulate_experiment  # imported here: it pulls in numpy and the simulator models
        registry = self.metric_registry()
        others = tuple(sorted({registry.get(i["item_id"], i["item_version"]).product_id for i in self.get_design(experiment_id)
                               if i["kind"] == "metric"} - {exp["product_id"]}))     # metrics from other products need their activity too
        try:
            sim = simulate_experiment(runner.wh, experiment_id, exp["product_id"], n_users, exp["runtime_days"] - 6, multipliers,
                                      0.5, seed, start=exp["launch_date"], extra_products=others)
        except ValueError as e:
            raise errors.InvalidInput(str(e)) from None
        self.save_ground_truth(actor, experiment_id, sim.truth)
        self._audit(actor.user_id, "simulation.land", "experiment", experiment_id,
                    {"n_users": len(sim.cohort), "multipliers": multipliers, "also_simulated": list(others)})
        return {"n_users": len(sim.cohort)}

    def land_september_demo(self, actor: Actor, progress=None) -> list[str]:
        """Dev only: land the fixed September 2026 demo experiments (users assigned on every day from 1 to 30 September), so a demo
        experiment can use any launch and end date inside the month. Safe to repeat. Returns the experiment ids."""
        runner = self._need_runner()
        from p2.simulator.demo import land_september
        ids = land_september(runner.wh, progress)
        self._audit(actor.user_id, "simulation.september_demo", "platform", "september-2026", {"experiments": ids})
        return ids

    def list_jobs(self, experiment_id: str) -> list[dict]:
        return self.store.select("job_runs", {"experiment_id": experiment_id}, order_by=["started_at DESC"])

    def job_steps(self, job_id: str) -> list[dict]:
        return self.store.select("job_steps", {"job_id": job_id})

    def experiment_frame(self, experiment_id: str):
        """The built per-user table for an experiment, as a DataFrame."""
        return self._need_runner().load_final(experiment_id)

    # ---- simulator ground truth (validation only, never read by the analysis path) -----------
    def save_ground_truth(self, actor: Actor, experiment_id: str, truth: dict[str, dict[str, float]]) -> None:
        exp = self.get_experiment(experiment_id)
        require_edit_experiment(actor, exp["owner_user_id"])
        self.store.delete("sim_ground_truth", {"experiment_id": experiment_id})
        self.store.insert_many("sim_ground_truth", [
            {"experiment_id": experiment_id, "metric_id": m, "true_control": float(t["control"]),
             "true_variant": float(t["variant"]), "true_lift": float(t["relative_lift"])} for m, t in truth.items()])
        self._audit(actor.user_id, "simulation.truth", "experiment", experiment_id, {"metrics": len(truth)})

    def load_ground_truth(self, experiment_id: str) -> dict[str, dict[str, float]]:
        rows = self.store.select("sim_ground_truth", {"experiment_id": experiment_id})
        return {r["metric_id"]: {"control": r["true_control"], "variant": r["true_variant"],
                                 "relative_lift": r["true_lift"]} for r in rows}

    # ---- audit -----------------------------------------------------------------------------
    def list_audit(self, actor: Actor, entity_id: str | None = None, limit: int = 200) -> list[dict]:
        require_admin(actor)
        rows = self.store.select("audit_log", {"entity_id": entity_id} if entity_id else None, order_by=["ts DESC"])
        return rows[:limit]
