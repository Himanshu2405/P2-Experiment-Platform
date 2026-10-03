"""Application state tables (see TECH_SPEC.md section 4)."""
from p2.store.base import Col, TableDef

S, I, F, B, TS, D, J = "STRING", "INT64", "FLOAT64", "BOOL", "TIMESTAMP", "DATE", "JSON"


def _t(name: str, key: tuple[str, ...], *cols: tuple) -> TableDef:
    return TableDef(name, tuple(Col(*c) for c in cols), key)


STAMPS = (("created_at", TS), ("updated_at", TS))

APP_TABLES: dict[str, TableDef] = {t.name: t for t in [
    _t("team_members", ("user_id",), ("user_id", S), ("name", S), ("role", S), ("product_id", S, True),
       ("active", B), *STAMPS),
    _t("products", ("product_id",), ("product_id", S), ("name", S), ("owner_user_id", S, True), *STAMPS),
    _t("data_sources", ("source_id",), ("source_id", S), ("product_id", S), ("raw_table", S), ("staging_view", S),
       ("dedupe_key", J), ("dedupe_order", S), ("time_column", S), ("user_column", S), ("description", S),
       ("owner", S, True), ("status", S), ("schema", J), *STAMPS),
    _t("catalog_items", ("item_id", "version"), ("item_id", S), ("version", I), ("kind", S), ("product_id", S),
       ("display_name", S), ("description", S), ("category", S, True), ("sql", S), ("status", S), ("author", S),
       ("reviewed_by", S, True), ("reviewed_at", TS, True), ("review_note", S, True), ("value_type", S, True),
       ("aggregation", S, True), ("good_direction", S, True), ("format", S, True),
       ("default_value", S, True), ("qa_report", J, True), *STAMPS),
    _t("experiments", ("experiment_id",), ("experiment_id", S), ("product_id", S), ("owner_user_id", S),
       ("name", S), ("hypothesis", S), ("status", S), ("launch_date", D), ("runtime_days", I), ("end_date", D, True),
       *STAMPS, ("decision", S, True), ("decision_notes", S, True), ("decided_by", S, True),
       ("decided_at", TS, True)),
    _t("experiment_items", ("experiment_id", "item_id"), ("experiment_id", S), ("item_id", S),
       ("item_version", I), ("kind", S), ("role", S), ("baseline", F, True), ("std", F, True),
       ("effect", F, True), ("effect_kind", S, True), ("alpha", F, True), ("power", F, True),
       ("sidedness", S, True)),
    _t("results", ("experiment_id", "item_id", "run_at"), ("experiment_id", S), ("item_id", S), ("run_at", TS),
       ("role", S), ("mean_control", F, True), ("mean_variant", F, True), ("difference", F, True),
       ("ci_low", F, True), ("ci_high", F, True), ("p_value", F, True), ("verdict", S, True),
       ("achieved_power", F, True), ("n_control", I, True), ("n_variant", I, True), ("relative_lift", F, True),
       ("ci_level", F, True), ("kind", S, True), ("through_date", D, True), ("params", J, True)),
    _t("daily_stats", ("experiment_id", "item_id", "day", "arm"), ("experiment_id", S), ("item_id", S), ("day", D), ("arm", S),
       ("n_users", I), ("mean_value", F), ("var_value", F, True), ("run_at", TS)),
    _t("segment_results", ("experiment_id", "item_id", "dimension_id", "segment", "filter_id"), ("experiment_id", S), ("item_id", S),
       ("dimension_id", S), ("segment", S), ("filter_id", S), ("run_at", TS), ("kind", S), ("role", S), ("mean_control", F, True),
       ("mean_variant", F, True), ("difference", F, True), ("relative_lift", F, True), ("ci_low", F, True), ("ci_high", F, True),
       ("ci_level", F, True), ("p_value", F, True), ("verdict", S, True), ("n_control", I), ("n_variant", I), ("through_date", D)),
    _t("job_runs", ("job_id",), ("job_id", S), ("experiment_id", S), ("type", S), ("status", S),
       ("started_at", TS), ("finished_at", TS, True), ("error", S, True), ("detail", J, True)),
    _t("job_steps", ("job_id", "step"), ("job_id", S), ("step", S), ("table_name", S, True), ("status", S),
       ("row_count", I, True), ("duration_s", F, True)),
    _t("audit_log", ("audit_id",), ("audit_id", S), ("ts", TS), ("actor", S), ("action", S),
       ("entity_type", S), ("entity_id", S), ("detail", J, True)),
    _t("sim_ground_truth", ("experiment_id", "metric_id"), ("experiment_id", S), ("metric_id", S),
       ("true_control", F), ("true_variant", F), ("true_lift", F)),
]}
