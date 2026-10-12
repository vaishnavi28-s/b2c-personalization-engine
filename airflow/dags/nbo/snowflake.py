"""
Calling the Snowflake procedures from Airflow.

Every procedure answers with JSON {"status": ..., "error": ..., ...}. What Airflow does with it:
  SUCCESS / SKIPPED            -> task succeeds
  WARN                         -> task succeeds, the warning is logged (Grafana shows it from NBO_RUN_LOG)
  PENDING                      -> task succeeds (evaluation: the 90 days are not over yet)
  FAILED + an error message    -> something crashed (network, Snowflake hiccup) -> normal failure, Airflow RETRIES
  FAILED without an error      -> a rule said no (data check, output check, blocked render) -> fail WITHOUT retry:
                                  running it again gives the same answer, a person has to look

Each call is tagged (QUERY_TAG = dag / task / run) so its cost is visible in Snowflake's query history, and has
a Snowflake-side time limit just below the Airflow task's limit, so a killed task never leaves a query running.
"""
import json
import logging

from nbo.config import SNOWFLAKE_CONN

log = logging.getLogger(__name__)
OK = {"SUCCESS", "SKIPPED", "PENDING"}


class RuleFailed(Exception):
    """A business rule stopped the step (no retry)."""


def hook(timeout_seconds=3600, tag="nbo"):
    from airflow.providers.snowflake.hooks.snowflake import SnowflakeHook
    return SnowflakeHook(snowflake_conn_id=SNOWFLAKE_CONN,
                         session_parameters={"QUERY_TAG": tag[:2000],
                                             "STATEMENT_TIMEOUT_IN_SECONDS": int(timeout_seconds)})


def query_tag():
    try:
        from airflow.sdk import get_current_context
        ctx = get_current_context()
        return f"airflow:{ctx['dag'].dag_id}:{ctx['task'].task_id}:{ctx['run_id']}"
    except Exception:
        return "airflow"


def interpret(name, answer):
    """Turn a procedure's JSON answer into: return the dict, or raise (retryable or not)."""
    result = json.loads(answer) if isinstance(answer, str) else dict(answer)
    status = str(result.get("status", "")).upper()
    if status in OK:
        log.info("%s %s: %s", name, status, result)
        return result
    if status == "WARN":
        log.warning("%s WARN: %s", name, result)
        return result
    if status == "FAILED" and result.get("error"):
        raise RuntimeError(f"{name} crashed: {result['error']}")
    if status == "FAILED":
        from airflow.exceptions import AirflowFailException
        raise AirflowFailException(f"{name} stopped by a rule: {json.dumps(result, default=str)[:2000]}")
    raise RuntimeError(f"{name} returned an unknown status: {result}")


def call(name, *args, timeout_seconds=3600):
    """CALL <name>(args) in the schema of the connection; returns the procedure's answer as a dict."""
    marks = ", ".join(["%s"] * len(args))
    row = hook(timeout_seconds - 60, query_tag()).get_first(f"CALL {name}({marks})", parameters=args)
    if not row:
        raise RuntimeError(f"{name} returned nothing")
    return interpret(name, row[0])


def query(sql, parameters=None, timeout_seconds=600):
    """A SELECT -> pandas DataFrame (column names as Snowflake returns them, upper case)."""
    return hook(timeout_seconds, query_tag()).get_df(sql, parameters=parameters)


def execute(sql, parameters=None, timeout_seconds=600):
    """One statement; returns the number of rows changed (used to claim an email before sending it)."""
    conn = hook(timeout_seconds, query_tag()).get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, parameters)
        n = cur.rowcount
        cur.close()
        conn.commit()
        return n
    finally:
        conn.close()
