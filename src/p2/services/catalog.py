"""Catalog service: metrics, dimensions and filters as governed items (Draft, In review, Certified, Deprecated).

Mixed into Platform. The rules live here: who may propose, edit, review and retire; that automated checks must
pass (and be current) before review; that a certified version never changes; and that an author cannot certify
their own item.
"""
import hashlib
import json
import re
from datetime import datetime

from p2.catalog.seed import SEED_ITEMS
from p2.registry.registry import MetricDef, Registry
from p2.services import errors
from p2.services.permissions import (Actor, require_edit_item, require_manage_item, require_propose_item,
                                     require_review_item)
from p2.store import base as store_errors

KINDS = ("metric", "dimension", "filter")
ITEM_ID = re.compile(r"[a-z][a-z0-9_]{1,40}")
RESERVED = {"user_id", "arm", "assigned_date", "value", "metric_date", "effective_date"}
FORMATS = ("percent", "number", "currency")
PRODUCTS = ("checkout", "email", "onboarding")
MAX_SQL = 10_000
EDITABLE = ("display_name", "description", "category", "sql", "value_type", "aggregation", "good_direction", "format",
            "default_value")
FINGERPRINTED = ("sql", "kind", "product_id", "value_type", "aggregation", "default_value")


def fingerprint(item: dict) -> str:
    """Hash of everything the automated checks depend on; a changed item needs fresh checks."""
    payload = json.dumps({k: item.get(k) for k in FINGERPRINTED}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _seed_row(s, now: datetime) -> dict:
    return {
        "item_id": s.item_id, "version": 1, "kind": s.kind, "product_id": s.product_id, "display_name": s.display_name,
        "description": s.description, "category": None, "sql": s.sql, "status": "Certified", "author": "system",
        "reviewed_by": "system", "reviewed_at": now, "review_note": "Seeded starter item",
        "value_type": s.value_type, "aggregation": s.aggregation, "good_direction": s.good_direction, "format": s.format,
        "default_value": s.default_value,
        "qa_report": {"seeded": True, "passed": True,
                      "note": "Seeded as Certified; the automated checks are verified against real data in the test suite."},
        "created_at": now, "updated_at": now,
    }


def seed_rows(now: datetime) -> list[dict]:
    return [_seed_row(s, now) for s in SEED_ITEMS]


class CatalogMixin:
    # ---- reading -----------------------------------------------------------------------------
    def list_items(self, kind: str | None = None, product_id: str | None = None, status: str | None = None,
                   search: str | None = None, latest_only: bool = True) -> list[dict]:
        where = {k: v for k, v in (("kind", kind), ("product_id", product_id), ("status", status)) if v}
        rows = self.store.select("catalog_items", where, order_by=["item_id", "version"])
        if latest_only:
            best: dict[str, dict] = {}
            for r in rows:
                best[r["item_id"]] = r  # ascending order: the last version wins
            rows = list(best.values())
        if search:
            q = search.lower()
            rows = [r for r in rows if q in r["item_id"] or q in r["display_name"].lower() or q in r["description"].lower()]
        return rows

    def get_item(self, item_id: str, version: int | None = None) -> dict:
        if version is not None:
            row = self.store.get("catalog_items", {"item_id": item_id, "version": version})
        else:
            rows = self.store.select("catalog_items", {"item_id": item_id}, order_by=["version DESC"])
            row = rows[0] if rows else None
        if row is None:
            raise errors.NotFound(f"unknown catalog item: {item_id}" + (f" version {version}" if version else ""))
        return row

    def item_versions(self, item_id: str) -> list[dict]:
        return self.store.select("catalog_items", {"item_id": item_id}, order_by=["version"])

    def certified_items(self, kind: str, product_id: str | None = None) -> list[dict]:
        """Latest Certified version per item, for dropdowns. Dimensions and filters include shared ones."""
        rows = self.store.select("catalog_items", {"kind": kind, "status": "Certified"}, order_by=["item_id", "version"])
        best: dict[str, dict] = {}
        for r in rows:
            best[r["item_id"]] = r
        out = list(best.values())
        if product_id:
            out = [r for r in out if r["product_id"] in (product_id, "shared")]
        return out

    def metric_registry(self) -> Registry:
        """Every Certified or Deprecated metric version, as the power calculation sees it."""
        rows = [r for r in self.store.select("catalog_items", {"kind": "metric"}) if r["status"] in ("Certified", "Deprecated")]
        return Registry([MetricDef(
            metric_id=r["item_id"], version=r["version"], product_id=r["product_id"], display_name=r["display_name"],
            description=r["description"], type=r["value_type"], window_aggregation=r["aggregation"],
            good_direction=r["good_direction"], status=r["status"]) for r in rows])

    def item_usage(self, item_id: str) -> list[dict]:
        """Experiments that use this item (any version), with the pinned version and role."""
        out = []
        for u in self.store.select("experiment_items", {"item_id": item_id}):
            exp = self.store.get("experiments", {"experiment_id": u["experiment_id"]})
            if exp:
                out.append({"experiment_id": u["experiment_id"], "name": exp["name"], "status": exp["status"],
                            "item_version": u["item_version"], "role": u["role"]})
        return sorted(out, key=lambda r: r["experiment_id"])

    # ---- validation --------------------------------------------------------------------------
    def _validate_item_fields(self, kind: str, f: dict) -> None:
        for name in ("display_name", "description", "sql"):
            if name in f and not str(f[name] or "").strip():
                raise errors.InvalidInput(f"{name.replace('_', ' ')} is required")
        if "sql" in f and len(f["sql"]) > MAX_SQL:
            raise errors.InvalidInput(f"the SQL is longer than {MAX_SQL} characters")
        if kind == "metric":
            if f.get("value_type") is not None and f["value_type"] not in ("binary", "continuous"):
                raise errors.InvalidInput("value type must be binary or continuous")
            if f.get("aggregation") is not None and f["aggregation"] not in ("max", "sum"):
                raise errors.InvalidInput("aggregation must be max or sum")
            if f.get("good_direction") is not None and f["good_direction"] not in ("higher", "lower"):
                raise errors.InvalidInput("good direction must be higher or lower")
            if f.get("format") is not None and f["format"] not in FORMATS:
                raise errors.InvalidInput(f"format must be one of {list(FORMATS)}")

    def _validate_item_complete(self, item: dict) -> None:
        kind = item["kind"]
        self._validate_item_fields(kind, item)
        if kind == "metric":
            missing = [k for k in ("value_type", "aggregation", "good_direction", "format") if not item.get(k)]
            if missing:
                raise errors.InvalidInput(f"a metric needs: {', '.join(missing)}")
            if item["value_type"] == "binary" and item["aggregation"] != "max":
                raise errors.InvalidInput("a binary metric must use the max aggregation")
        elif kind == "dimension":
            if not (item.get("default_value") or "").strip():
                raise errors.InvalidInput("a dimension needs a default value (for users with no row at entry), e.g. Unknown")

    # ---- proposing and editing ---------------------------------------------------------------
    def propose_item(self, actor: Actor, kind: str, item_id: str, product_id: str, display_name: str, description: str,
                     sql: str, *, value_type: str | None = None, aggregation: str | None = None,
                     good_direction: str | None = None, format: str | None = None,
                     default_value: str | None = None, category: str | None = None) -> dict:
        if kind not in KINDS:
            raise errors.InvalidInput(f"kind must be one of {list(KINDS)}")
        if not ITEM_ID.fullmatch(item_id) or item_id in RESERVED:
            raise errors.InvalidInput("item id must be lowercase letters, digits and underscores, starting with a letter "
                                      f"(2 to 41 characters), and not one of {sorted(RESERVED)}")
        valid_products = PRODUCTS if kind == "metric" else (*PRODUCTS, "shared")
        if product_id not in valid_products:
            raise errors.InvalidInput("a metric belongs to one product" if kind == "metric" else f"product must be one of {list(valid_products)}")
        require_propose_item(actor, product_id)
        if self.store.select("catalog_items", {"item_id": item_id}):
            raise errors.InvalidInput(f"{item_id} already exists; propose a new version of it instead")
        now = self.clock()
        row = {"item_id": item_id, "version": 1, "kind": kind, "product_id": product_id, "display_name": display_name.strip(),
               "description": description.strip(), "category": category, "sql": sql.strip(), "status": "Draft",
               "author": actor.user_id, "reviewed_by": None, "reviewed_at": None, "review_note": None,
               "value_type": value_type, "aggregation": aggregation, "good_direction": good_direction, "format": format,
               "default_value": default_value,
               "qa_report": None, "created_at": now, "updated_at": now}
        self._validate_item_complete(row)
        try:
            self.store.insert("catalog_items", row)
        except store_errors.AlreadyExists:
            raise errors.InvalidInput(f"{item_id} already exists") from None
        self._audit(actor.user_id, "catalog.propose", "catalog_item", item_id, {"kind": kind, "product": product_id})
        return row

    def update_draft(self, actor: Actor, item_id: str, version: int, **changes) -> dict:
        item = self.get_item(item_id, version)
        require_edit_item(actor, item["author"])
        if item["status"] != "Draft":
            raise errors.InvalidInput(f"{item_id} v{version} is {item['status']}; only drafts can be edited")
        bad = set(changes) - set(EDITABLE)
        if bad:
            raise errors.InvalidInput(f"cannot change {sorted(bad)}")
        if not changes:
            raise errors.InvalidInput("no changes given")
        merged = {**item, **changes}
        self._validate_item_complete(merged)
        if "sql" in changes:
            changes = {**changes, "sql": changes["sql"].strip()}
        row = self.store.update("catalog_items", {"item_id": item_id, "version": version}, changes)
        self._audit(actor.user_id, "catalog.update", "catalog_item", item_id, {"version": version, "fields": sorted(changes)})
        return row

    def new_version(self, actor: Actor, item_id: str) -> dict:
        """Start the next draft version from the latest one. Certified versions never change; this is how they evolve."""
        latest = self.get_item(item_id)
        require_propose_item(actor, latest["product_id"])
        if latest["status"] in ("Draft", "In review"):
            raise errors.InvalidInput(f"{item_id} v{latest['version']} is still {latest['status']}; finish it first")
        now = self.clock()
        row = {**latest, "version": latest["version"] + 1, "status": "Draft", "author": actor.user_id, "reviewed_by": None,
               "reviewed_at": None, "review_note": None, "qa_report": None, "created_at": now, "updated_at": now}
        self.store.insert("catalog_items", row)
        self._audit(actor.user_id, "catalog.new_version", "catalog_item", item_id, {"version": row["version"]})
        return row

    # ---- checks, review, retirement ----------------------------------------------------------
    def run_checks(self, actor: Actor, item_id: str, version: int | None = None) -> dict:
        item = self.get_item(item_id, version)
        if actor.user_id != item["author"]:
            try:
                require_manage_item(actor, item["product_id"])
            except errors.PermissionDenied:
                require_edit_item(actor, item["author"])
        if self.checker is None:
            raise errors.InvalidInput("no warehouse is connected, so the automated checks cannot run")
        report = dict(self.checker.check(item))
        report["fingerprint"] = fingerprint(item)
        report["ran_at"] = self.clock().isoformat()
        self.store.update("catalog_items", {"item_id": item_id, "version": item["version"]}, {"qa_report": report})
        self._audit(actor.user_id, "catalog.checks", "catalog_item", item_id,
                    {"version": item["version"], "passed": bool(report.get("passed"))})
        return report

    def submit_for_review(self, actor: Actor, item_id: str, version: int) -> dict:
        item = self.get_item(item_id, version)
        require_edit_item(actor, item["author"])
        if item["status"] != "Draft":
            raise errors.InvalidInput(f"{item_id} v{version} is {item['status']}, not a draft")
        report = item["qa_report"]
        if not report:
            raise errors.InvalidInput("run the automated checks before submitting")
        if report.get("fingerprint") != fingerprint(item):
            raise errors.InvalidInput("the item changed since the checks ran; run them again")
        if not report.get("passed"):
            raise errors.InvalidInput("the automated checks failed; fix the problems and run them again")
        row = self.store.update("catalog_items", {"item_id": item_id, "version": version}, {"status": "In review"})
        self._audit(actor.user_id, "catalog.submit", "catalog_item", item_id, {"version": version})
        return row

    def certify(self, actor: Actor, item_id: str, version: int, note: str | None = None) -> dict:
        item = self.get_item(item_id, version)
        require_review_item(actor, item["product_id"], item["author"])
        if item["status"] != "In review":
            raise errors.InvalidInput(f"{item_id} v{version} is {item['status']}; only items In review can be certified")
        now = self.clock()
        row = self.store.update("catalog_items", {"item_id": item_id, "version": version},
                                {"status": "Certified", "reviewed_by": actor.user_id, "reviewed_at": now, "review_note": note})
        for other in self.item_versions(item_id):  # the new version supersedes older certified ones for new experiments
            if other["version"] != version and other["status"] == "Certified":
                self.store.update("catalog_items", {"item_id": item_id, "version": other["version"]}, {"status": "Deprecated"})
        self._audit(actor.user_id, "catalog.certify", "catalog_item", item_id, {"version": version})
        return row

    def reject(self, actor: Actor, item_id: str, version: int, note: str) -> dict:
        item = self.get_item(item_id, version)
        require_review_item(actor, item["product_id"], item["author"])
        if item["status"] != "In review":
            raise errors.InvalidInput(f"{item_id} v{version} is {item['status']}, not In review")
        if not (note or "").strip():
            raise errors.InvalidInput("say why it is being sent back")
        row = self.store.update("catalog_items", {"item_id": item_id, "version": version},
                                {"status": "Draft", "reviewed_by": actor.user_id, "reviewed_at": self.clock(), "review_note": note.strip()})
        self._audit(actor.user_id, "catalog.reject", "catalog_item", item_id, {"version": version})
        return row

    def deprecate(self, actor: Actor, item_id: str, version: int) -> dict:
        item = self.get_item(item_id, version)
        require_manage_item(actor, item["product_id"])
        if item["status"] != "Certified":
            raise errors.InvalidInput(f"{item_id} v{version} is {item['status']}; only Certified versions can be deprecated")
        row = self.store.update("catalog_items", {"item_id": item_id, "version": version}, {"status": "Deprecated"})
        self._audit(actor.user_id, "catalog.deprecate", "catalog_item", item_id, {"version": version})
        return row
