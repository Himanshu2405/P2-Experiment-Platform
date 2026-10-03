"""The experiment form, shared by the New experiment page (create) and the Experiment catalog (edit). One page of fields:
Experiment, Metrics, Audience, then Save and Save and run."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st

from common import current_actor, get_platform, get_registry, read, refresh
from p2.design_form import describe, describe_bayes, to_bayes_plan, to_bayes_ui, to_plan, to_ui
from p2.pipeline.runner import end_of, last_day
from p2.services.errors import PlatformError
from p2.stats.bayes import (BayesPlan, DEFAULT_HARM_LIMIT, DEFAULT_MEAN_THRESHOLD, DEFAULT_MIN_DAYS, DEFAULT_RATE_THRESHOLD)
from p2.stats.plan import MetricPlan

FIELD_KEYS = ("exp_id", "owner", "name", "hypothesis", "launch", "runtime", "end", "_launch_for", "product", "filters", "dimensions", "guardrails", "secondary",
              "primary_metric", "method")
PLAN_PREFIXES = ("primary_", "guardrail_")
ss = st.session_state

def _form_keys() -> list[str]:
    return [k for k in ss if k in FIELD_KEYS or k.startswith(PLAN_PREFIXES)]


def _restore() -> None:
    """Streamlit drops a widget's value when you visit another page. The snapshot is kept in a plain session key (which it does not
    drop) at the end of every run and put back here, so what was typed survives looking at other pages."""
    for k, v in (ss.get("_form_snapshot") or {}).items():
        if k not in ss:
            ss[k] = v


def _reset() -> None:
    for k in _form_keys():
        del ss[k]
    for k in ("_orig_end", "_orig_status", "_orig_method", "_form_snapshot", "_form_mode"):
        ss.pop(k, None)


def _snapshot() -> None:
    ss["_form_snapshot"] = {k: ss[k] for k in _form_keys()}


def _set_plan_keys(prefix: str, metric, row: dict) -> None:
    ui = to_ui(metric, row)
    ss[f"{prefix}_baseline"] = float(round(ui["baseline"], 6))
    if ui["std"] is not None:
        ss[f"{prefix}_std"] = float(round(ui["std"], 6))
    ss[f"{prefix}_kind"] = "Relative (%)" if ui["kind"] == "relative" else "Absolute"
    ss[f"{prefix}_effect_{ui['kind']}"] = float(round(ui["effect"], 6))
    ss[f"{prefix}_alpha"] = float(ui["alpha"])
    ss[f"{prefix}_power"] = float(ui["power"])
    if prefix.startswith("primary"):
        ss[f"{prefix}_sided"] = ui["sidedness"]


def _set_bayes_keys(prefix: str, metric, row: dict) -> None:
    ui = to_bayes_ui(metric, row)
    if "threshold" in ui:
        ss[f"{prefix}_bthreshold"] = float(round(ui["threshold"], 6))
        ss[f"{prefix}_bmindays"] = int(ui["min_days"])
    else:
        ss[f"{prefix}_bkind"] = "Relative (%)" if ui["kind"] == "relative" else "Absolute"
        ss[f"{prefix}_bmargin_{ui['kind']}"] = float(round(ui["margin"], 6))
        ss[f"{prefix}_bharm"] = float(round(ui["harm_limit"], 6))


def _load(experiment_id: str) -> None:
    """Fill the form from a saved experiment and its plan, in any status. Everything but the id can then be changed."""
    _reset()
    experiments = read("list_experiments", None, None, None)
    exp = next((e for e in experiments if e["experiment_id"] == experiment_id), None)
    if exp is None:
        return
    ss.update(exp_id=experiment_id, name=exp["name"], hypothesis=exp["hypothesis"], launch=exp["launch_date"],
              runtime=exp["runtime_days"], end=exp["end_date"], _launch_for=experiment_id)
    ss["_orig_end"], ss["_orig_status"] = end_of(exp), exp["status"]
    ss["_orig_method"] = "Bayesian" if exp.get("method") == "bayesian" else "Frequentist"
    ss["owner"], ss["product"] = exp["owner_user_id"], exp["product_id"]
    bayesian = exp.get("method") == "bayesian"
    ss["method"] = "Bayesian" if bayesian else "Frequentist"
    set_keys = _set_bayes_keys if bayesian else _set_plan_keys
    registry = get_registry()
    design = read("get_design", experiment_id)
    guards, secondary = [], []
    for r in design:
        if r["kind"] != "metric":
            continue
        if r["role"] == "primary":
            ss["primary_metric"] = r["item_id"]
            set_keys(f"primary_{r['item_id']}", registry.get(r["item_id"]), r)
        elif r["role"] == "guardrail":
            guards.append(r["item_id"])
            set_keys(f"guardrail_{r['item_id']}", registry.get(r["item_id"]), r)
        elif r["role"] == "secondary":
            secondary.append(r["item_id"])
    ss["guardrails"], ss["secondary"] = guards, secondary
    for kind, key in (("filter", "filters"), ("dimension", "dimensions")):
        allowed = {i["item_id"] for i in read("certified_items", kind, exp["product_id"])}
        ss[key] = [r["item_id"] for r in design if r["kind"] == kind and r["item_id"] in allowed]


if target := ss.pop("edit_exp", None):
    _load(target)

def render(edit_id: str | None = None, title: bool = True) -> None:
    """Draw the form: blank for a new experiment, or loaded from `edit_id`. Everything except the id can be changed."""
    registry = get_registry()
    actor = current_actor()
    plat = get_platform()

    editing = edit_id
    mode = f"edit:{edit_id}" if edit_id else "new"
    if ss.get("_form_mode") != mode:      # a different form than last time (new versus an experiment): start from its own values
        _reset()
        if edit_id:
            _load(edit_id)
        ss["_form_mode"] = mode            # set last: loading resets the form, which clears the mode
    else:
        _restore()
    if title:
        st.title(f"Edit {editing}" if editing else "Add Experiment")
    if message := ss.pop("form_msg", None):
        st.success(message)

    existing = read("list_experiments", None, None, None)
    if editing:
        st.caption("Everything except the ID can be changed. Saving updates the experiment; run it again to refresh the numbers.")


    # ------------------------------------------------------------------ the statistical inputs for one metric
    def stat_inputs(metric_id: str, role: str) -> MetricPlan | None:
        """The data scientist's numbers for one primary or guardrail metric. Returns None until they are complete and valid."""
        with st.container(border=True):
            metric = registry.get(metric_id)
            binary = metric.type == "binary"
            k = f"{role}_{metric_id}"
            unit = "%" if binary else "$"
            st.markdown(f"**{metric.display_name}** &nbsp;|&nbsp; {'Rate' if binary else 'Continuous'} &nbsp;|&nbsp; {metric.product_id} &nbsp;|&nbsp; "
                        f"{'lower' if metric.good_direction == 'lower' else 'higher'} is better",
                        help="The type comes from the catalog and decides the test: rates use a z-test, continuous metrics a Welch t-test.")
            c = st.columns(4)
            baseline = c[0].number_input(f"Baseline ({unit})", min_value=0.0, value=None, format="%.4f", key=f"{k}_baseline",
                                         placeholder="e.g. 3.5" if binary else "e.g. 12.50",
                                         help="Expected control value from your own calculation." + (" Enter a rate as a percent." if binary else ""))
            sd = c[1].number_input("Std dev ($)" if not binary else "Std dev", min_value=0.0, value=None, format="%.4f", key=f"{k}_std",
                                   placeholder="not needed for rates" if binary else "e.g. 27", disabled=binary,
                                   help="Needed for continuous metrics. Not used for rates, where it follows from the baseline.")
            ss.setdefault(f"{k}_kind", "Relative (%)")
            kind_label = c[2].radio("Effect type", ["Relative (%)", "Absolute"], horizontal=True, key=f"{k}_kind",
                                    help="Relative is a percent of the baseline; absolute is in the metric's own units (percentage points for rates).")
            kind = "relative" if kind_label.startswith("Relative") else "absolute"
            effect_label = ("Minimum detectable effect" if role == "primary" else "Max acceptable worsening") + (
                " (%)" if kind == "relative" else (" (pp)" if binary else " ($)"))
            effect = c[3].number_input(effect_label, min_value=0.0, value=None, format="%.4f", key=f"{k}_effect_{kind}", placeholder="e.g. 10",
                                       help="The smallest improvement you planned to detect." if role == "primary"
                                       else "The largest worsening you accept before this guardrail fails.")
            c2 = st.columns(3)
            ss.setdefault(f"{k}_alpha", 0.05)
            ss.setdefault(f"{k}_power", 0.80)
            alpha = c2[0].number_input("Alpha", min_value=0.001, max_value=0.499, format="%.3f", key=f"{k}_alpha",
                                       help="Significance level used in the final test.")
            power = c2[1].number_input("Power (1 - beta)", min_value=0.5, max_value=0.999, format="%.3f", key=f"{k}_power",
                                       help="The power you planned for. Recorded for reference.")
            ss.setdefault(f"{k}_sided", "two-sided")
            if role == "primary":
                sidedness = c2[2].radio("Test", ["two-sided", "one-sided"], horizontal=True, key=f"{k}_sided",
                                        help="Two-sided looks for any change; one-sided only for an improvement.")
            else:
                c2[2].markdown("Test: **one-sided non-inferiority**", help="Guardrails only ask whether the metric got worse by more than your margin.")
                sidedness = "one-sided"
            missing = [n for n, v in (("baseline", baseline), ("effect", effect)) if v is None]
            if not binary and sd is None:
                missing.append("standard deviation")
            if missing:
                st.info("Enter the " + ", ".join(missing) + " to complete this metric.")
                return None
            try:
                plan = to_plan(metric, role, baseline, sd, effect, kind, alpha, power, sidedness)
                st.caption(describe(plan))
                return plan
            except ValueError as e:
                st.error(str(e))
                return None


    def bayes_inputs(metric_id: str, role: str) -> BayesPlan | None:
        """The data scientist's Bayesian numbers for one primary or guardrail metric (all have defaults). None until they are valid."""
        with st.container(border=True):
            metric = registry.get(metric_id)
            binary = metric.type == "binary"
            k = f"{role}_{metric_id}"
            st.markdown(f"**{metric.display_name}** &nbsp;|&nbsp; {'Rate' if binary else 'Continuous'} &nbsp;|&nbsp; {metric.product_id} &nbsp;|&nbsp; "
                        f"{'lower' if metric.good_direction == 'lower' else 'higher'} is better")
            if role == "primary":
                c = st.columns(2)
                ss.setdefault(f"{k}_bthreshold", DEFAULT_RATE_THRESHOLD * 100 if binary else DEFAULT_MEAN_THRESHOLD * 100)
                ss.setdefault(f"{k}_bmindays", DEFAULT_MIN_DAYS)
                threshold = c[0].number_input("Risk threshold (pp)" if binary else "Risk threshold (% of control average)", min_value=0.0, format="%.4f",
                                              key=f"{k}_bthreshold", help="The most you are willing to lose on average if you pick the wrong arm. The verdict "
                                              "compares the risk of shipping the variant and the risk of keeping control with it.")
                min_days = int(c[1].number_input("Minimum days before a verdict", min_value=1, max_value=365, step=1, key=f"{k}_bmindays",
                                                 help="Numbers show from day one, but no verdict is given before this many days of data."))
                try:
                    plan = to_bayes_plan(metric, role, threshold, min_days, None, "relative", None)
                    st.caption(describe_bayes(plan))
                    return plan
                except ValueError as e:
                    st.error(str(e))
                    return None
            c = st.columns(3)
            ss.setdefault(f"{k}_bkind", "Relative (%)")
            ss.setdefault(f"{k}_bharm", DEFAULT_HARM_LIMIT * 100)
            kind_label = c[0].radio("Margin type", ["Relative (%)", "Absolute"], horizontal=True, key=f"{k}_bkind",
                                    help="Relative is a percent of the control average; absolute is in the metric's own units (percentage points for rates).")
            kind = "relative" if kind_label.startswith("Relative") else "absolute"
            margin = c[1].number_input("Margin" + (" (%)" if kind == "relative" else (" (pp)" if binary else " ($)")), min_value=0.0, value=None,
                                       format="%.4f", key=f"{k}_bmargin_{kind}", placeholder="e.g. 1",
                                       help="How much worse the metric can get before it counts as harm.")
            harm = c[2].number_input("Harm limit (%)", min_value=0.01, max_value=49.9, format="%.2f", key=f"{k}_bharm",
                                     help="The guardrail passes when the chance of harm is below this. Strict by default: 1%.")
            if margin is None:
                st.info("Enter the margin to complete this guardrail.")
                return None
            try:
                plan = to_bayes_plan(metric, role, None, None, margin, kind, harm)
                st.caption(describe_bayes(plan))
                return plan
            except ValueError as e:
                st.error(str(e))
                return None


    def metric_label(metric_id: str) -> str:
        m = registry.get(metric_id)
        return f"{m.display_name} ({m.product_id})"


    # ------------------------------------------------------------------ the experiment
    with st.container(border=True):
        st.subheader("Experiment")
        ss.setdefault("launch", date.today())
        ss.setdefault("runtime", 14)
        ss.setdefault("end", None)
        exp_id = st.text_input("Experiment ID", key="exp_id", disabled=bool(editing), placeholder="exactly as in the experiment tool",
                               help="The id from the experiment tool's assignment log. It cannot be changed later.").strip()
        seen = {}
        if not editing:
            if not exp_id:
                st.info("Enter the experiment ID to start.")
                return _snapshot()
            if any(e["experiment_id"] == exp_id for e in existing):
                st.info(f"{exp_id} already exists. Open or edit it in the Experiment Catalog.")
                return _snapshot()
            seen = read("assignment_check", exp_id)
            if not seen.get("n_users"):
                st.error(f"{exp_id} does not exist in the experiment tool's assignment log. Come back once the experiment is live.")
                return _snapshot()
            if ss.get("_launch_for") != exp_id:        # first time this id is seen: start from its first assignment day (still editable)
                ss["launch"], ss["_launch_for"] = seen["first_day"], exp_id
            st.success(f"Found {exp_id} in the assignment log: {seen['n_users']:,} users so far, first assigned {seen['first_day']}.")
        team = [m for m in read("list_team") if m["active"] and m["role"] != "viewer"]
        owner = st.selectbox("Owner", [m["user_id"] for m in team], index=None, placeholder="Select your name", key="owner",
                             format_func=lambda u: next(m["name"] for m in team if m["user_id"] == u),
                             help="The person who owns this experiment. Pick your own name.")
        products = [p["product_id"] for p in read("list_products")]
        if owner and ss.get("product") is None:        # start from the owner's own product, still changeable
            ss["product"] = next((m["product_id"] for m in team if m["user_id"] == owner and m["product_id"] in products), None)
        product = st.selectbox("Product", products, index=None, placeholder="Select a product", key="product",
                               help="The product team this experiment belongs to. Filled from the owner; change it if needed.")
        name = st.text_input("Experiment name", key="name", placeholder="Free-shipping banner", help="A short label for people, shown in the catalog.")
        c = st.columns(3)
        launch = c[0].date_input("Launch date", key="launch", help="The first day of the experiment. Filled from the first assignment in the log; change it if you want.")
        runtime = int(c[1].number_input("Runtime (days)", min_value=1, max_value=365, step=1, key="runtime",
                                        help="The planned runtime from your power calculation, counting the launch day."))
        end = c[2].date_input("End date (optional)", value=None, key="end",
                              help="Leave empty to stop after the planned runtime. Numbers and charts stop on this day, even if it is later than the runtime.")
        planned_end = last_day(launch, runtime)
        st.caption(f"Planned last day: {planned_end}" + (f". Ends {end} ({(end - launch).days + 1} days in total)." if end and end != planned_end else ""))
        if editing and ss.get("_orig_status") in ("Running", "Analyzed", "Decided") and (end or planned_end) > ss.get("_orig_end", planned_end):
            st.warning("You are extending an experiment that already has results. The verdict will use data up to the new end date. Looking at results "
                       "first and then deciding to run longer raises the chance of a false positive.")
        hypothesis = st.text_area("Hypothesis (optional)", key="hypothesis", placeholder="If we show a free-shipping banner, more visitors will complete a purchase.",
                                  help="What you expect to happen and why.")

        # ------------------------------------------------------------------ metrics
    with st.container(border=True):
        st.subheader("Metrics", help="Any metric can be primary, a guardrail or secondary. A metric picked in one role is not offered in the others.")
        ss.setdefault("method", "Frequentist")
        method = st.radio("Method", ["Frequentist", "Bayesian"], horizontal=True, key="method",
                          help="Frequentist: p-values, confidence intervals and a verdict after the last day. Bayesian: the chance the variant is better "
                               "and the risk of each choice, live after a minimum number of days. Only the inputs for the chosen method are shown.")
        bayesian = method == "Bayesian"
        inputs = bayes_inputs if bayesian else stat_inputs
        if editing and ss.get("_orig_status") in ("Running", "Analyzed", "Decided") and (ss.get("_orig_method") or "Frequentist") != method:
            st.warning("You are changing the method of an experiment that already has results. Run it again to see the numbers in the new method.")
        all_ids = [m.metric_id for m in registry.all_metrics()]
        if ss.get("primary_metric") is not None and ss["primary_metric"] not in all_ids:
            del ss["primary_metric"]
        taken = {ss.get("primary_metric")}  # a metric is offered in one role only; drop any stale pick that now clashes
        ss["guardrails"] = [m for m in (ss.get("guardrails") or []) if m in all_ids and m not in taken]
        taken |= set(ss["guardrails"])
        ss["secondary"] = [m for m in (ss.get("secondary") or []) if m in all_ids and m not in taken]
        guard_now, sec_now = set(ss["guardrails"]), set(ss["secondary"])
        primary_id = st.selectbox("Primary metric", [i for i in all_ids if i not in guard_now | sec_now], index=None,
                                  placeholder="Choose a metric", format_func=metric_label, key="primary_metric",
                                  help="The one metric the decision is based on.")
        primary = inputs(primary_id, "primary") if primary_id else None
        guard_ids = st.multiselect("Guardrail metrics", [i for i in all_ids if i != primary_id and i not in sec_now], format_func=metric_label,
                                   key="guardrails", help="Metrics that must not get worse by more than your margin.")
        guards = [inputs(g, "guardrail") for g in guard_ids]
        sec_ids = st.multiselect("Secondary metrics", [i for i in all_ids if i != primary_id and i not in set(guard_ids)], format_func=metric_label,
                                 key="secondary", help="Extra metrics to learn from. No inputs; shown without a verdict.")

        # ------------------------------------------------------------------ audience (optional)
    with st.container(border=True):
        st.subheader("Audience (optional)", help="Everyone assigned during the runtime is always analysed. Segments and filters add views to the results.")
        flt = read("certified_items", "filter", product) if product else []
        dim = read("certified_items", "dimension", product) if product else []
        filter_ids = st.multiselect("Filters", [f["item_id"] for f in flt], format_func=lambda i: next(f["display_name"] for f in flt if f["item_id"] == i),
                                    key="filters", help="Adds a view of the results for the users who pass the filter. The main table still shows everyone.")
        dimension_ids = st.multiselect("Dimensions", [d["item_id"] for d in dim], format_func=lambda i: next(d["display_name"] for d in dim if d["item_id"] == i),
                                       key="dimensions", help="Adds segments to split the results by, for example free and paid users.")

        # ------------------------------------------------------------------ save
    st.divider()
    needs = []
    if not owner:
        needs.append("an owner")
    if not product:
        needs.append("a product")
    if not name.strip():
        needs.append("an experiment name")
    if primary_id is None:
        needs.append("a primary metric")
    elif primary is None:
        needs.append("the primary metric's numbers")
    needs += [f"the numbers for {metric_label(g)}" for g, p in zip(guard_ids, guards) if p is None]
    if needs:
        st.caption("Still needed for a complete plan: " + ", ".join(needs) + ".")


    def _done(message: str) -> None:
        """Show the message on the next run. A new experiment is saved, so the form starts blank for the next one; an edit stays open."""
        if not editing:
            _reset()
        ss["form_msg"] = message
        st.rerun()


    def save(run: bool) -> None:
        try:
            with st.spinner("Building the numbers from BigQuery. This can take a minute..." if run else "Saving..."):
                if not name.strip():
                    raise PlatformError("Give the experiment a name first.")
                if not owner or not product:
                    raise PlatformError("Select the owner and the product first.")
                if editing or any(e["experiment_id"] == exp_id for e in existing):
                    plat.update_experiment(actor, exp_id, name, hypothesis, launch, runtime, end_date=end, owner_user_id=owner, product_id=product)
                else:
                    plat.create_experiment(actor, exp_id, product, name, hypothesis, launch, runtime, end, owner)
                complete = primary is not None and all(g is not None for g in guards)
                if not complete:
                    refresh()
                    _done(f"Saved {exp_id} as a draft. " + ("Still needed: " + ", ".join(needs) + "." if needs else ""))
                plat.save_design(actor, exp_id, primary, list(guards), sec_ids, dimension_ids, filter_ids)
                refresh()
                if not run:
                    _done(f"Saved {exp_id}. Edit it any time from the Experiment catalog, or open its numbers under Experiments.")
                ticket = plat.start_run(actor, exp_id)       # queued: it runs in the background, you follow it on the results page
                refresh()
                _reset()  # the experiment is saved and the run has started; the next visit to this form starts fresh
                ss["open_target"] = exp_id
                kind = "the final analysis" if ticket["kind"] == "final" else "the live numbers so far"
                if ticket["state"] == "succeeded":            # a server set up to run in the caller's thread
                    ss["flash"] = f"Saved and ran {exp_id}: {ticket['result'].audience_users:,} users in {kind}."
                else:
                    ss["flash"] = f"Saved {exp_id}. {kind.capitalize()} is being built in the background; follow it below."
                try:
                    st.switch_page("views/experiments.py")
                except Exception:  # not available outside the full app (for example in tests); the message below still shows
                    st.success(ss.pop("flash"))
        except (PlatformError, ValueError, KeyError) as e:
            refresh()
            st.error(str(e))


    c = st.columns([1, 1, 5])
    if c[0].button("Save", key="save", help="Saves what you have so far. A plan that is not complete is kept as a draft."):
        save(False)
    if c[1].button("Save and run", type="primary", key="save_run", help="Saves the plan, then builds the numbers: live so far while the "
                                                                        "experiment is running, or the final analysis once it has ended."):
        save(True)

    _snapshot()
