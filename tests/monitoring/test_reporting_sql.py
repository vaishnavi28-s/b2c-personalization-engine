"""
Runs the reporting database set-up and EVERY dashboard and alert query against a real Postgres
(a service container in CI). A typo in a column name would otherwise only show as a broken panel / a silent alert.

  REPORTING_TEST_DSN=postgresql://postgres:postgres@localhost:5432/postgres pytest tests/monitoring
"""
import glob
import json
import os
import re

import pytest
import yaml

psycopg2 = pytest.importorskip("psycopg2")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MON = os.path.join(ROOT, "monitoring")
DSN = os.environ.get("REPORTING_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="REPORTING_TEST_DSN not set (needs a Postgres)")

SAMPLE = """
INSERT INTO reporting.run_log VALUES
  ('r1', 'NBO_EVENTS_SENT', '2020-06-01', now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC', 12, 'SUCCESS', '{}', '',
   'SVC', 'abc123'),
  ('r2', 'NBO_TRAIN', '2020-06-01', now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC', 1200, 'FAILED', '{}', 'boom',
   'SVC', 'abc123');
INSERT INTO reporting.data_checks VALUES
  ('2020-06-01', 'volume_change_vs_4_weeks', 0.1, 'within +/-30%', 'PASS', '', now() AT TIME ZONE 'UTC'),
  ('2020-06-01', 'unknown_article_share', 0.02, '<= 0.01', 'FAIL', '', now() AT TIME ZONE 'UTC');
INSERT INTO reporting.model_registry VALUES ('2020-06-01', now() AT TIME ZONE 'UTC', 'FAIL', 'worse', '2020-05-17',
  0.02, 100, 0.03, 100, 0.03, 0.03, '2020-05-01', 1, 1, 1, 1, 12.5, 'abc123', 0.025, 0.01, false,
  'taste p@5 0.02 <= best sellers 0.025');
INSERT INTO reporting.run_metrics VALUES ('2020-06-01', 'slot1_fallback_rate', 0.1),
  ('2020-06-01', 'emails_complete_share', 1), ('2020-06-01', 'customers_email', 950);
INSERT INTO reporting.slot_mix VALUES ('2020-06-01', 5, 'model', 'AND', 300), ('2020-06-01', 6, 'model', 'OR', 100);
INSERT INTO reporting.outbox_status VALUES ('2020-06-01', 'SENT', 1), ('2020-06-01', 'SENDING', 2);
INSERT INTO reporting.events_daily VALUES ('2020-06-01', '2020-06-20', 'buy', 3);
INSERT INTO reporting.eval_results VALUES
  ('2020-06-01', 'slot', '1', 'buy_rate', 0.01, 0.0, 0.02, 950),
  ('2020-06-01', 'email', 'all', 'email_buy_rate', 0.05, 0.03, 0.07, 950),
  (CURRENT_DATE - 10, 'holdout', 'email_minus_holdout', 'any_buy_rate', -0.02, -0.03, -0.01, 1000),
  ('2020-06-01', 'holdout_pooled', 'email_minus_holdout', 'any_buy_rate', 0.001, -0.004, 0.006, 6000),
  ('2020-06-01', 'holdout_pooled', 'holdout_level', 'any_buy_rate', 0.62, NULL, NULL, 300),
  ('2020-06-01', 'holdout_pooled', 'min_detectable_lift', 'any_buy_rate', 0.008, NULL, NULL, 6000);
INSERT INTO reporting.deployments VALUES ('abc123', now(), 'CI', 'run 1');
INSERT INTO reporting.export_log (table_name, rows_exported) VALUES ('run_log', 2);
"""
# what each alert must say on the sample rows above (1 = firing)
EXPECTED_ALERTS = {"nbo-step-failed": 1, "nbo-send-late": None, "nbo-emails-stuck": 2,
                   "nbo-export-stale": 0, "nbo-data-check-failed": 1, "nbo-data-check-warn": 0,
                   "nbo-gate-failed": 1, "nbo-model-below-baseline": 1, "nbo-emails-hurt": 1}


@pytest.fixture(scope="module")
def db():
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute("DROP DATABASE IF EXISTS reporting_test")
    cur.execute("CREATE DATABASE reporting_test")
    for role in ("nbo_writer", "grafana_reader"):
        cur.execute(f"DO $$BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') "
                    f"THEN CREATE ROLE {role} LOGIN; END IF; END$$")
    cur.execute("GRANT CONNECT, CREATE ON DATABASE reporting_test TO nbo_writer")
    conn = psycopg2.connect(psycopg2.extensions.make_dsn(DSN, dbname="reporting_test"))
    conn.autocommit = True
    c = conn.cursor()
    c.execute(open(os.path.join(MON, "postgres", "init.sql")).read())
    c.execute(SAMPLE)
    yield c
    conn.close()
    cur.execute("DROP DATABASE IF EXISTS reporting_test")
    admin.close()


def time_filter(sql):
    return re.sub(r"\$__timeFilter\(([\w.]+)\)", r"\1 BETWEEN '2019-01-01' AND '2100-01-01'", sql)


def panels():
    for f in sorted(glob.glob(os.path.join(MON, "grafana", "dashboards", "*.json"))):
        for p in json.load(open(f))["panels"]:
            for t in p.get("targets", []):
                if "rawSql" in t:
                    yield pytest.param(t["rawSql"], t["format"], id=f"{os.path.basename(f)}:{p['title']}")


@pytest.mark.parametrize("sql,fmt", list(panels()))
def test_dashboard_query_runs(db, sql, fmt):
    db.execute(time_filter(sql))
    cols = [d[0] for d in db.description]
    db.fetchall()
    if fmt == "time_series":
        assert cols[0] == "time", cols                                  # Grafana needs the time column first


def alert_queries():
    doc = yaml.safe_load(open(os.path.join(MON, "grafana", "provisioning", "alerting", "rules.yml")))
    for g in doc["groups"]:
        for r in g["rules"]:
            for q in r["data"]:
                if q["datasourceUid"] == "reporting":
                    yield pytest.param(r["uid"], q["model"]["rawSql"], id=r["uid"])


@pytest.mark.parametrize("uid,sql", list(alert_queries()))
def test_alert_query_returns_one_number_and_fires_correctly(db, uid, sql):
    db.execute(sql)
    rows = db.fetchall()
    assert len(rows) == 1 and len(rows[0]) == 1, rows
    if EXPECTED_ALERTS.get(uid) is not None:
        assert rows[0][0] == EXPECTED_ALERTS[uid], (uid, rows[0][0])


def test_grafana_reader_cannot_write(db):
    db.execute("SELECT has_table_privilege('grafana_reader', 'reporting.run_log', 'INSERT'), "
               "has_table_privilege('grafana_reader', 'reporting.run_log', 'SELECT')")
    assert db.fetchone() == (False, True)
