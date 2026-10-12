"""
DAG tests, run inside the same Airflow image that runs in production (CI builds it, then runs pytest in it).
  - every DAG file imports without errors (a broken DAG disappears from Airflow silently otherwise)
  - production rules for every task: owner, retries, time limit, Snowflake pool
  - the send schedule produces EXACTLY the send dates of NBO_SEND_CALENDAR (Airflow and Snowflake agree)
  - how procedure answers are handled (retry vs stop), and the claim-once email sending
"""
import os
import re

import pandas as pd
import pendulum
import pytest

os.environ.setdefault("NBO_ALERT_EMAIL", "alerts@example.com")      # so the failure email is configured
os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DAGS = os.path.join(ROOT, "airflow", "dags")

from airflow.dag_processing.dagbag import DagBag  # noqa: E402
from airflow.exceptions import AirflowFailException  # noqa: E402
from airflow.timetables.base import DataInterval, TimeRestriction  # noqa: E402

from nbo import email_sender, reporting  # noqa: E402
from nbo import snowflake as sf  # noqa: E402

EXPECTED = {
    "nbo_send": ["wait_for_data", "data_checks", "dev_rehearsal", "train", "recommend", "render", "send_emails",
                 "record_sent"],
    "nbo_evaluate": ["due_dates", "evaluate"],
    "nbo_reporting_export": ["export"],
}


@pytest.fixture(scope="module")
def bag():
    return DagBag(dag_folder=DAGS)


def test_no_import_errors(bag):
    assert bag.import_errors == {}


def test_expected_dags_and_task_order(bag):
    assert set(bag.dag_ids) == set(EXPECTED)
    send = bag.dags["nbo_send"]
    order = [t.task_id for t in send.topological_sort()] if hasattr(send, "topological_sort") else None
    chain = EXPECTED["nbo_send"]
    for a, b in zip(chain, chain[1:]):
        assert b in send.get_task(a).downstream_task_ids, f"{a} -> {b} missing"
    if order:
        assert order == chain


def test_every_task_follows_the_production_rules(bag):
    for dag in bag.dags.values():
        assert dag.tags and "nbo" in dag.tags, dag.dag_id
        assert dag.max_active_runs == 1, dag.dag_id
        assert dag.dagrun_timeout is not None, dag.dag_id
        assert dag.on_failure_callback, f"{dag.dag_id}: no failure email"
        for t in dag.tasks:
            assert t.owner == "nbo", (dag.dag_id, t.task_id)
            assert t.retries >= 1, (dag.dag_id, t.task_id)
            assert t.execution_timeout is not None or "sensor" in type(t).__name__.lower(), (dag.dag_id, t.task_id)


def test_snowflake_work_is_limited_by_the_pool(bag):
    outside = {"wait_for_data", "dev_rehearsal", "send_emails"}         # waiting / SMTP: no Snowflake slot held
    for dag in bag.dags.values():
        for t in dag.tasks:
            if t.task_id not in outside:
                assert t.pool == "snowflake", (dag.dag_id, t.task_id, t.pool)


def test_send_dag_replays_in_order(bag):
    send = bag.dags["nbo_send"]
    assert send.catchup is True
    assert all(t.depends_on_past for t in send.tasks)
    sensors = [t for t in send.tasks if t.task_id in ("wait_for_data", "dev_rehearsal")]
    assert all(getattr(t, "mode", "") == "reschedule" for t in sensors)       # waiting never blocks a worker


def calendar_dates():
    text = open(os.path.join(ROOT, "snowflake", "tables", "config_tables.sql")).read()
    block = text.split("INSERT INTO NBO_SEND_CALENDAR VALUES", 1)[1].split(";", 1)[0]
    return re.findall(r"\('(\d{4}-\d{2}-\d{2})'", block)


def test_schedule_matches_the_send_calendar(bag):
    send = bag.dags["nbo_send"]
    tt, tz = send.timetable, pendulum.timezone("Europe/Berlin")
    restriction = TimeRestriction(earliest=send.start_date, latest=send.end_date, catchup=True)
    dates, last = [], None
    while True:
        info = tt.next_dagrun_info(last_automated_data_interval=last, restriction=restriction)
        if info is None:
            break
        dates.append(pendulum.instance(info.logical_date).in_timezone(tz).strftime("%Y-%m-%d"))
        last = DataInterval(info.data_interval.start, info.data_interval.end)
    assert dates == calendar_dates()


# ---------------- procedure answers ----------------
def test_success_warn_skipped_pending_pass():
    for s in ("SUCCESS", "WARN", "SKIPPED", "PENDING"):
        assert sf.interpret("P", f'{{"status": "{s}"}}')["status"] == s


def test_crash_is_retried_rule_is_not():
    with pytest.raises(RuntimeError, match="crashed"):
        sf.interpret("P", '{"status": "FAILED", "error": "ConnectionError: reset"}')
    with pytest.raises(AirflowFailException, match="stopped by a rule"):
        sf.interpret("P", '{"status": "FAILED", "error": "", "failed_checks": ["cap_broken=1"]}')
    with pytest.raises(RuntimeError, match="unknown status"):
        sf.interpret("P", '{"status": "MAYBE"}')


# ---------------- email sending ----------------
class Outbox:
    """The outbox table as the sender sees it (claim = conditional update)."""
    def __init__(self, rows):
        self.rows = {r["EMAIL_ID"]: dict(r) for r in rows}

    def query(self, sql, params=None, **kw):
        live = [r for r in self.rows.values() if r["STATUS"] in ("READY", "FAILED")]
        return pd.DataFrame(live, columns=["EMAIL_ID", "RECIPIENT_EMAIL", "SUBJECT", "HTML", "STATUS"])

    def execute(self, sql, params=None, **kw):
        if "SET STATUS = 'SENDING'" in sql:
            r = self.rows[params[1]]
            if r["STATUS"] in ("READY", "FAILED"):
                r["STATUS"] = "SENDING"
                return 1
            return 0
        status, _, error, _, eid, _ = params
        if self.rows[eid]["STATUS"] == "SENDING":
            self.rows[eid].update(STATUS=status, ERROR=error)
            return 1
        return 0


class Smtp:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    def __call__(self, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send_email_smtp(self, to, subject, html_content, from_email, **kw):
        if to in self.fail_for:
            raise ConnectionError("smtp refused")
        self.sent.append(to)


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox([dict(EMAIL_ID=f"E{i}", RECIPIENT_EMAIL=f"r{i}@example.com", SUBJECT="s", HTML="<p>", STATUS=s)
                  for i, s in enumerate(["READY", "READY", "SENT", "SENDING", "FAILED"])])
    monkeypatch.setattr(email_sender.sf, "query", box.query)
    monkeypatch.setattr(email_sender.sf, "execute", box.execute)
    monkeypatch.setattr(email_sender, "EMAIL_FROM", "nbo@example.com")
    return box


def test_sends_ready_and_failed_once_never_sent_or_sending(outbox, monkeypatch):
    smtp = Smtp()
    monkeypatch.setattr("airflow.providers.smtp.hooks.smtp.SmtpHook", smtp)
    assert email_sender.send_all("2020-06-01")["sent"] == 3
    assert sorted(smtp.sent) == ["r0@example.com", "r1@example.com", "r4@example.com"]
    assert [outbox.rows[e]["STATUS"] for e in ("E0", "E1", "E2", "E3", "E4")] == ["SENT", "SENT", "SENT", "SENDING",
                                                                                  "SENT"]
    smtp.sent.clear()
    assert email_sender.send_all("2020-06-01")["sent"] == 0 and smtp.sent == []     # a retry sends nothing twice


def test_a_failed_email_is_marked_and_the_task_fails(outbox, monkeypatch):
    smtp = Smtp(fail_for={"r1@example.com"})
    monkeypatch.setattr("airflow.providers.smtp.hooks.smtp.SmtpHook", smtp)
    with pytest.raises(RuntimeError, match="1 emails failed"):
        email_sender.send_all("2020-06-01")
    assert outbox.rows["E1"]["STATUS"] == "FAILED" and "smtp refused" in outbox.rows["E1"]["ERROR"]
    assert outbox.rows["E0"]["STATUS"] == "SENT"


# ---------------- reporting export ----------------
def test_every_export_has_a_postgres_table():
    ddl = open(os.path.join(ROOT, "monitoring", "postgres", "init.sql")).read()
    for name in reporting.EXPORTS:
        assert f"CREATE TABLE IF NOT EXISTS reporting.{name} (" in ddl, name


def test_export_columns_match_the_postgres_tables():
    """Each SELECT's output columns == the Postgres table's columns (a mismatch would fail the export)."""
    ddl = open(os.path.join(ROOT, "monitoring", "postgres", "init.sql")).read()
    for name, sql in reporting.EXPORTS.items():
        body = ddl.split(f"CREATE TABLE IF NOT EXISTS reporting.{name} (", 1)[1].split(");", 1)[0]
        pg_cols = [line.strip().split()[0] for line in body.strip().splitlines()
                   if line.strip() and not line.strip().startswith(("--", "PRIMARY", "UNIQUE"))]
        select = sql.split("SELECT", 1)[1].split("FROM", 1)[0] if "FROM (" not in sql else ""
        parts, depth, cur = [], 0, ""
        for ch in select:
            depth += ch == "("
            depth -= ch == ")"
            if ch == "," and depth == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        sf_cols = [re.split(r"\s+AS\s+", p.strip(), flags=re.I)[-1].strip().split(".")[-1].lower() for p in parts]
        assert sf_cols == pg_cols, (name, sf_cols, pg_cols)
