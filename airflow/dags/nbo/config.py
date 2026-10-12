"""
Settings for the DAGs, in two kinds:

1. Deployment settings: environment variables from .env (same for every run, change = restart)
     NBO_EMAIL_FROM    sender address of the emails
     NBO_ALERT_EMAIL   who gets "a pipeline run failed" emails
2. Operating switches: Airflow Variables (change in the UI, no restart, every change is in the audit log)
     nbo_send_enabled           true/false  - false = everything runs, emails stay READY (e.g. SMTP problem)
     nbo_customers_per_send     5000
     nbo_require_dev_rehearsal  true/false  - on retrain dates wait for the monthly DEV rehearsal to pass
"""
import os

import pendulum

TZ = pendulum.timezone("Europe/Berlin")
SNOWFLAKE_CONN = "snowflake_nbo"          # Airflow connection -> NBO_REBUILD.HM_ML as NBO_AIRFLOW_SVC (key pair)
POSTGRES_CONN = "postgres_reporting"      # Airflow connection -> the reporting database Grafana reads
SMTP_CONN = "smtp_nbo"                    # Airflow connection -> mail server
SNOWFLAKE_POOL = "snowflake"              # at most 2 Snowflake jobs at the same time (created by airflow-init)
DEV_SCHEMA = "HM_ML_DEV"                  # where the monthly rehearsal runs (GitHub Actions)

EMAIL_FROM = os.environ.get("NBO_EMAIL_FROM", "")
ALERT_EMAIL = os.environ.get("NBO_ALERT_EMAIL", "")


def variable(name, default):
    """An Airflow Variable read at run time (never at parse time), with a typed default."""
    from airflow.sdk import Variable
    value = Variable.get(name, default=None)
    if value is None:
        return default
    if isinstance(default, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(value)
    return value


def required(name, value):
    if not value:
        raise ValueError(f"{name} is not set: add it to the .env file and restart Airflow")
    return value
