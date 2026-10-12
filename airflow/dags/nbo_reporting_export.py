"""
nbo_reporting_export - copies the report tables from Snowflake to the Postgres reporting database (Grafana).

Runs when new results exist (a send or an evaluation finished - Airflow assets) and every hour
as a safety net. One task per table, so one broken table does not block the others; each table is replaced in
one transaction (see nbo/reporting.py).
"""
from datetime import timedelta

import pendulum
from airflow.sdk import Asset, dag, task
from airflow.timetables.assets import AssetOrTimeSchedule
from airflow.timetables.trigger import CronTriggerTimetable

from nbo import reporting
from nbo.common import DEFAULT_ARGS, failure_email
from nbo.config import SNOWFLAKE_POOL, TZ

UPDATED = Asset("nbo://snowflake/hm_ml/send") | Asset("nbo://snowflake/hm_ml/evaluation")


@dag(
    dag_id="nbo_reporting_export",
    description="Snowflake report tables -> Postgres for Grafana",
    schedule=AssetOrTimeSchedule(timetable=CronTriggerTimetable("15 * * * *", timezone=TZ), assets=UPDATED),
    start_date=pendulum.datetime(2026, 10, 1, tz=TZ),
    catchup=False,
    max_active_runs=1,
    default_args={**DEFAULT_ARGS, "pool": SNOWFLAKE_POOL, "execution_timeout": timedelta(minutes=10)},
    dagrun_timeout=timedelta(minutes=45),
    on_failure_callback=failure_email(),
    tags=["nbo", "monitoring"],
    doc_md=__doc__,
)
def nbo_reporting_export():

    @task
    def export(table: str):
        return reporting.export_table(table)

    export.expand(table=sorted(reporting.EXPORTS))


nbo_reporting_export()
