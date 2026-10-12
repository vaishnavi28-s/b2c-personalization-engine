"""
Copies the report tables from Snowflake to the Postgres reporting database that Grafana reads.

Why a copy: dashboards refresh every minute and alerts query every few minutes; pointing them at Snowflake would
wake the warehouse all day (cost). The tables are small (one row per run / check / metric), so each export is a
full refresh inside ONE transaction: Grafana sees either the old table or the new one, never half of it.
Every export writes a row to reporting.export_log - Grafana alerts when the copy is stale.

Each entry: Postgres table -> Snowflake SELECT whose columns match monitoring/postgres/init.sql (types converted
here, so Grafana gets real dates and timestamps). The HTML of the emails is never copied.
"""
import logging

import pandas as pd

from nbo import snowflake as sf
from nbo.config import POSTGRES_CONN

log = logging.getLogger(__name__)

TS = "TRY_TO_TIMESTAMP_TZ({})::TIMESTAMP_NTZ"   # stored as text in Snowflake, real timestamps (UTC) in Postgres

EXPORTS = {
    "run_log": f"""
        SELECT RUN_ID, PROCEDURE_NAME, TRY_TO_DATE(RUN_DATE) AS RUN_DATE,
               {TS.format('START_TIME')} AS START_TIME, {TS.format('END_TIME')} AS END_TIME,
               DURATION_SECONDS, STATUS, LEFT(DETAIL, 2000) AS DETAIL, LEFT(ERROR_MESSAGE, 2000) AS ERROR_MESSAGE,
               RUN_BY, CODE_VERSION
        FROM NBO_RUN_LOG""",
    "data_checks": f"""
        SELECT TRY_TO_DATE(RUN_DATE) AS RUN_DATE, CHECK_NAME, VALUE, EXPECTED, STATUS, DETAIL,
               {TS.format('CHECKED_AT')} AS CHECKED_AT
        FROM NBO_DATA_CHECKS""",
    "model_registry": f"""
        SELECT TRY_TO_DATE(MODEL_DATE) AS MODEL_DATE, {TS.format('TRAINED_AT')} AS TRAINED_AT, GATE_STATUS,
               GATE_REASON, TRY_TO_DATE(CHECK_TRAINED_TO) AS CHECK_TRAINED_TO, TASTE_P5, TASTE_CHECK_CUSTOMERS,
               BASKET_P5, BASKET_CHECK_CUSTOMERS, PREV_TASTE_P5, PREV_BASKET_P5, TRY_TO_DATE(MODEL_USED) AS MODEL_USED,
               TASTE_CUSTOMERS, TASTE_ITEMS, BASKET_CUSTOMERS, BASKET_ITEMS, MINUTES, CODE_VERSION,
               TASTE_BASE_P5, BASKET_BASE_P5, BEATS_BASELINE, BASELINE_NOTE
        FROM NBO_MODEL_REGISTRY""",
    "run_metrics": """
        SELECT TRY_TO_DATE(RUN_DATE) AS RUN_DATE, METRIC, VALUE FROM NBO_RUN_METRICS""",
    "slot_mix": """
        SELECT TRY_TO_DATE(RUN_DATE) AS RUN_DATE, POS AS SLOT, SOURCE, RULE, COUNT(*) AS CARDS
        FROM NBO_RECOMMENDATION_ITEMS WHERE GRP = 'EMAIL' GROUP BY 1, 2, 3, 4""",
    "outbox_status": """
        SELECT TRY_TO_DATE(RUN_DATE) AS RUN_DATE, STATUS, COUNT(*) AS EMAILS
        FROM NBO_EMAIL_OUTBOX GROUP BY 1, 2""",
    "events_daily": """
        SELECT TRY_TO_DATE(RUN_DATE) AS RUN_DATE, TRY_TO_TIMESTAMP_NTZ(EVENT_TIME)::DATE AS EVENT_DAY, EVENT_TYPE,
               COUNT(*) AS EVENTS
        FROM NBO_EMAIL_EVENTS GROUP BY 1, 2, 3""",
    "eval_results": """
        SELECT TRY_TO_DATE(RUN_DATE) AS RUN_DATE, GROUP_BY, GROUP_KEY, METRIC, VALUE, CI_LOW, CI_HIGH, N
        FROM NBO_EVAL_RESULTS""",
    "deployments": """
        SELECT CODE_VERSION, DEPLOYED_AT, DEPLOYED_BY, SOURCE FROM NBO_DEPLOYMENTS""",
}


def export_table(name):
    """Snowflake -> Postgres reporting.<name>, replaced in one transaction. Returns the row count."""
    from airflow.providers.postgres.hooks.postgres import PostgresHook
    from sqlalchemy import text

    df = sf.query(EXPORTS[name])
    df.columns = [c.lower() for c in df.columns]
    df = df.astype(object).where(pd.notna(df), None)
    engine = PostgresHook(postgres_conn_id=POSTGRES_CONN).get_sqlalchemy_engine()
    with engine.begin() as con:                                   # one transaction: all or nothing
        con.execute(text(f"TRUNCATE reporting.{name}"))
        if len(df):
            df.to_sql(name, con, schema="reporting", if_exists="append", index=False, method="multi", chunksize=1000)
        con.execute(text("INSERT INTO reporting.export_log (table_name, rows_exported, exported_at) "
                         "VALUES (:t, :n, NOW())"), {"t": name, "n": len(df)})
        con.execute(text("DELETE FROM reporting.export_log WHERE table_name = :t "
                         "AND exported_at < NOW() - INTERVAL '30 days'"), {"t": name})
    log.info("exported %s: %s rows", name, len(df))
    return len(df)
