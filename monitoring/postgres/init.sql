-- ============================================================
-- Reporting database for Grafana (a copy of Snowflake's report tables, refreshed by nbo_reporting_export).
-- Owner nbo_writer (Airflow), read-only grafana_reader (Grafana). Created on the first start of the container.
-- Column order = the SELECTs in airflow/dags/nbo/reporting.py (a DAG test checks they match).
-- ============================================================
CREATE SCHEMA IF NOT EXISTS reporting AUTHORIZATION nbo_writer;
SET ROLE nbo_writer;

CREATE TABLE IF NOT EXISTS reporting.run_log (
    run_id text,
    procedure_name text,
    run_date date,
    start_time timestamp,
    end_time timestamp,
    duration_seconds numeric,
    status text,
    detail text,
    error_message text,
    run_by text,
    code_version text
);

CREATE TABLE IF NOT EXISTS reporting.data_checks (
    run_date date,
    check_name text,
    value double precision,
    expected text,
    status text,
    detail text,
    checked_at timestamp
);

CREATE TABLE IF NOT EXISTS reporting.model_registry (
    model_date date,
    trained_at timestamp,
    gate_status text,
    gate_reason text,
    check_trained_to date,
    taste_p5 double precision,
    taste_check_customers numeric,
    basket_p5 double precision,
    basket_check_customers numeric,
    prev_taste_p5 double precision,
    prev_basket_p5 double precision,
    model_used date,
    taste_customers numeric,
    taste_items numeric,
    basket_customers numeric,
    basket_items numeric,
    minutes double precision,
    code_version text,
    taste_base_p5 double precision,
    basket_base_p5 double precision,
    beats_baseline boolean,
    baseline_note text
);

CREATE TABLE IF NOT EXISTS reporting.run_metrics (
    run_date date,
    metric text,
    value double precision
);

CREATE TABLE IF NOT EXISTS reporting.slot_mix (
    run_date date,
    slot integer,
    source text,
    rule text,
    cards bigint
);

CREATE TABLE IF NOT EXISTS reporting.outbox_status (
    run_date date,
    status text,
    emails bigint
);

CREATE TABLE IF NOT EXISTS reporting.events_daily (
    run_date date,
    event_day date,
    event_type text,
    events bigint
);

CREATE TABLE IF NOT EXISTS reporting.eval_results (
    run_date date,
    group_by text,
    group_key text,
    metric text,
    value double precision,
    ci_low double precision,
    ci_high double precision,
    n bigint
);

CREATE TABLE IF NOT EXISTS reporting.deployments (
    code_version text,
    deployed_at timestamp,
    deployed_by text,
    source text
);

CREATE TABLE IF NOT EXISTS reporting.export_log (
    table_name text,
    rows_exported bigint,
    exported_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS run_log_start ON reporting.run_log (start_time);
CREATE INDEX IF NOT EXISTS run_log_date ON reporting.run_log (run_date);
CREATE INDEX IF NOT EXISTS data_checks_date ON reporting.data_checks (run_date);
CREATE INDEX IF NOT EXISTS run_metrics_date ON reporting.run_metrics (run_date, metric);
CREATE INDEX IF NOT EXISTS eval_results_date ON reporting.eval_results (run_date, group_by);
CREATE INDEX IF NOT EXISTS export_log_time ON reporting.export_log (table_name, exported_at);

RESET ROLE;
GRANT USAGE ON SCHEMA reporting TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO grafana_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE nbo_writer IN SCHEMA reporting GRANT SELECT ON TABLES TO grafana_reader;