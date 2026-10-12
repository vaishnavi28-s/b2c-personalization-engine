"""Things every NBO DAG shares: default task settings, the failure email, the send date of a run."""
from datetime import timedelta

import pendulum

from nbo.config import ALERT_EMAIL, SMTP_CONN, TZ

DEFAULT_ARGS = {
    "owner": "nbo",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,      # 5 min, then 10 min ...
    "max_retry_delay": timedelta(minutes=30),
    "execution_timeout": timedelta(minutes=30),
}


def failure_email():
    """One email when a DAG run FAILS (not on every retry). Independent of Grafana, so a broken monitoring
    stack cannot hide a failed send. Empty list when no address is configured (local tests)."""
    if not ALERT_EMAIL:
        return []
    from airflow.providers.smtp.notifications.smtp import SmtpNotifier
    return [SmtpNotifier(
        to=ALERT_EMAIL, smtp_conn_id=SMTP_CONN,
        subject="[NBO] {{ dag.dag_id }} failed for {{ dag_run.logical_date | ds if dag_run.logical_date else dag_run.run_id }}",
        html_content=("<p>DAG <b>{{ dag.dag_id }}</b>, run <code>{{ dag_run.run_id }}</code> failed.</p>"
                      "<p>Open Airflow, find the red task, read its log. Snowflake details are in NBO_RUN_LOG "
                      "(ERROR_MESSAGE). Fix, then clear the failed task to re-run: every step is safe to re-run.</p>"))]


def send_date(context):
    """The send date of this run as 'YYYY-MM-DD': the 'run_date' param of a manual run, else the scheduled date."""
    params = context.get("params") or {}
    if params.get("run_date"):
        return str(params["run_date"])
    logical = context["dag_run"].logical_date or context["dag_run"].run_after
    return pendulum.instance(logical).in_timezone(TZ).strftime("%Y-%m-%d")
