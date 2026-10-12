"""
nbo_evaluate - every day 07:00: score every send whose evaluation is not final yet.

  due_dates  send dates with stored recommendations whose latest NBO_EVALUATE is not SUCCESS
             (never evaluated, PENDING because the 90 days are not over, or FAILED)
  evaluate   one mapped task per date, ONE AT A TIME, oldest first: buys within 90 days per slot, position and
             email, emailed vs holdout for the send and pooled over all sends up to it -> NBO_EVAL_RESULTS.
             The pooled numbers of a date include every earlier date, so the order matters (no parallel runs).
             PENDING is a normal answer, not an error.
"""
from datetime import timedelta

import pendulum
from airflow.sdk import Asset, dag, task
from airflow.timetables.trigger import CronTriggerTimetable

from nbo import snowflake as sf
from nbo.common import DEFAULT_ARGS, failure_email
from nbo.config import SNOWFLAKE_POOL, TZ

EVAL_DONE = Asset("nbo://snowflake/hm_ml/evaluation")


@dag(
    dag_id="nbo_evaluate",
    description="Evaluation of past sends: buys per slot and position, holdout lift (pooled)",
    schedule=CronTriggerTimetable("0 7 * * *", timezone=TZ),
    start_date=pendulum.datetime(2026, 10, 1, tz=TZ),
    catchup=False,
    max_active_runs=1,
    default_args={**DEFAULT_ARGS, "pool": SNOWFLAKE_POOL},
    dagrun_timeout=timedelta(hours=6),
    on_failure_callback=failure_email(),
    tags=["nbo", "evaluation"],
    doc_md=__doc__,
)
def nbo_evaluate():

    @task(execution_timeout=timedelta(minutes=10))
    def due_dates():
        df = sf.query("""
            WITH sends AS (SELECT DISTINCT RUN_DATE FROM NBO_RECOMMENDATIONS),
                 last AS (SELECT RUN_DATE, STATUS FROM NBO_RUN_LOG WHERE PROCEDURE_NAME = 'NBO_EVALUATE'
                          QUALIFY ROW_NUMBER() OVER (PARTITION BY RUN_DATE ORDER BY START_TIME DESC) = 1)
            SELECT s.RUN_DATE FROM sends s LEFT JOIN last l ON l.RUN_DATE = s.RUN_DATE
            WHERE COALESCE(l.STATUS, 'NONE') <> 'SUCCESS'
            ORDER BY s.RUN_DATE""")
        dates = [str(d) for d in df["RUN_DATE"]]
        print(f"{len(dates)} send dates to evaluate: {dates}")
        return dates

    @task(execution_timeout=timedelta(hours=1), max_active_tis_per_dagrun=1, outlets=[EVAL_DONE])
    def evaluate(run_date: str):
        return sf.call("NBO_EVALUATE", run_date, 90, timeout_seconds=3600)

    evaluate.expand(run_date=due_dates())


nbo_evaluate()
