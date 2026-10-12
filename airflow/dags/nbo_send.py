"""
nbo_send - one newsletter send (the 1st and the 16th of each month, 06:00 Berlin time).

  wait_for_data -> data_checks -> dev_rehearsal -> train -> recommend -> render -> send_emails -> record_sent
                     FAIL=stop     (retrain dates,   gate FAIL =     output        blocked if     SMTP, each
                     WARN=go on     if switched on)  keep old models checks FAIL   recommend      email claimed
                                                     (WARN)          = stop        failed         once

Replay: start 2019-10-01, end 2020-09-01, catchup on -> Airflow itself runs every past send date in order, one at
a time (max_active_runs=1), and a date only starts when the previous date's same step succeeded (depends_on_past):
the 16th needs the models of the 1st and the 14-day contact rule needs the earlier sends.
Re-running a date (clear in the UI) is safe: every step replaces that date's rows.
Manual run for one date: trigger with {"run_date": "2020-06-01"}.
"""
from datetime import timedelta

import pendulum
from airflow.sdk import Asset, Param, dag, get_current_context, task
from airflow.timetables.trigger import CronTriggerTimetable

from nbo import email_sender
from nbo import snowflake as sf
from nbo.common import DEFAULT_ARGS, failure_email, send_date
from nbo.config import DEV_SCHEMA, SNOWFLAKE_POOL, TZ, variable

SEND_DONE = Asset("nbo://snowflake/hm_ml/send")      # tells the reporting export that new results exist


@dag(
    dag_id="nbo_send",
    description="Next-best-offer newsletter: checks, training, 6 picks per customer, emails",
    schedule=CronTriggerTimetable("0 6 1,16 * *", timezone=TZ),
    start_date=pendulum.datetime(2019, 10, 1, tz=TZ),
    end_date=pendulum.datetime(2020, 9, 2, tz=TZ),
    catchup=True,
    max_active_runs=1,
    default_args={**DEFAULT_ARGS, "depends_on_past": True, "pool": SNOWFLAKE_POOL},
    dagrun_timeout=timedelta(hours=10),
    on_failure_callback=failure_email(),
    params={"run_date": Param("", type="string", description="YYYY-MM-DD to run one date by hand; empty = scheduled date")},
    tags=["nbo", "send"],
    doc_md=__doc__,
)
def nbo_send():

    @task.sensor(poke_interval=timedelta(minutes=30), timeout=timedelta(hours=12), mode="reschedule",
                 pool="default_pool")
    def wait_for_data():
        """Purchases up to the day before the send have arrived (the nightly load upstream)."""
        d = send_date(get_current_context())
        r = sf.query("SELECT MAX(T_DAT) AS LAST_DAY FROM NBO_REBUILD.HM_ODS.HM_NBO_DWH_F_TRANSACTION "
                     "WHERE T_DAT <= %s", (d,))
        last = r["LAST_DAY"].iloc[0]
        ready = last is not None and str(last)[:10] >= str(pendulum.parse(d).subtract(days=1).date())
        print(f"send {d}: purchases loaded up to {last} -> {'ready' if ready else 'waiting'}")
        return ready

    @task(execution_timeout=timedelta(minutes=20))
    def data_checks():
        return sf.call("NBO_DATA_CHECKS", send_date(get_current_context()), timeout_seconds=20 * 60)

    @task.sensor(poke_interval=timedelta(minutes=30), timeout=timedelta(days=2), mode="reschedule",
                 pool="default_pool")
    def dev_rehearsal():
        """On retrain dates (switch nbo_require_dev_rehearsal): the monthly GitHub rehearsal in DEV must have
        passed for this date before production trains. Other dates / switch off: passes at once."""
        d = send_date(get_current_context())
        if not variable("nbo_require_dev_rehearsal", False):
            return True
        cal = sf.query("SELECT RETRAIN FROM NBO_SEND_CALENDAR WHERE SEND_DATE = %s", (d,))
        if cal.empty or not bool(cal["RETRAIN"].iloc[0]):
            return True
        ok = sf.query(f"SELECT COUNT(*) AS N FROM NBO_REBUILD.{DEV_SCHEMA}.NBO_RUN_LOG "
                      "WHERE PROCEDURE_NAME = 'CI_REHEARSAL' AND RUN_DATE = %s AND STATUS = 'SUCCESS'", (d,))
        return int(ok["N"].iloc[0]) > 0

    @task(execution_timeout=timedelta(hours=4), retries=1)
    def train():
        return sf.call("NBO_TRAIN", send_date(get_current_context()), timeout_seconds=4 * 3600)

    @task(execution_timeout=timedelta(hours=2))
    def recommend():
        n = variable("nbo_customers_per_send", 5000)
        return sf.call("NBO_RECOMMEND", send_date(get_current_context()), n, timeout_seconds=2 * 3600)

    @task(execution_timeout=timedelta(minutes=30))
    def render():
        return sf.call("NBO_RENDER_EMAILS", send_date(get_current_context()), timeout_seconds=30 * 60)

    @task(execution_timeout=timedelta(minutes=30), pool="default_pool")
    def send_emails():
        d = send_date(get_current_context())
        if not variable("nbo_send_enabled", True):
            print(f"nbo_send_enabled is off: emails for {d} stay READY")
            return {"sent": 0, "note": "sending switched off"}
        return email_sender.send_all(d)

    @task(execution_timeout=timedelta(minutes=20), outlets=[SEND_DONE])
    def record_sent():
        return sf.call("NBO_EVENTS", "sent", send_date(get_current_context()), timeout_seconds=20 * 60)

    (wait_for_data() >> data_checks() >> dev_rehearsal() >> train() >> recommend() >> render()
     >> send_emails() >> record_sent())


nbo_send()
