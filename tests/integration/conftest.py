"""
Integration tests: the REAL procedures on the REAL H&M data in Snowflake.

Where they run (never in production):
  - after every merge: a temporary schema HM_ML_CI_<run id>, deployed fresh by deploy.py, dropped afterwards
    (fewer training epochs via NBO_SETTINGS so it finishes in minutes - the rules are the same)
  - monthly rehearsal: HM_ML_DEV with the real settings, on the next date production will train

Settings (environment variables):
  NBO_TEST_SCHEMA     schema to test (required; HM_ML is refused)
  NBO_TEST_DATE       a RETRAIN date from NBO_SEND_CALENDAR (default 2020-06-01); its 16th is run too
  NBO_TEST_CUSTOMERS  customers per send (default 500)
  SNOWFLAKE_CONNECTION  named connection (laptop) - otherwise the SNOWFLAKE_* key-pair variables (CI)
"""
import json
import os

import pandas as pd
import pytest

from snowflake_conn import connect

PROD = {"HM_ML"}


class Snow:
    """Small helper around one connection: run SQL, call a procedure and read its JSON answer."""

    def __init__(self, conn, schema):
        self.conn, self.schema = conn, schema

    def df(self, sql, params=None):
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
            return pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])
        finally:
            cur.close()

    def run(self, sql, params=None):
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
        finally:
            cur.close()

    def call(self, procedure, *args):
        marks = ", ".join(["%s"] * len(args))
        value = self.df(f"CALL {self.schema}.{procedure}({marks})", args).iloc[0, 0]
        return json.loads(value)


@pytest.fixture(scope="session")
def schema():
    s = os.environ.get("NBO_TEST_SCHEMA", "").upper()
    if not s:
        pytest.skip("NBO_TEST_SCHEMA not set: integration tests need a Snowflake schema")
    if s in PROD:
        pytest.exit(f"Refusing to run tests against production schema {s}", returncode=2)
    return s


@pytest.fixture(scope="session")
def snow(schema):
    conn = connect(os.environ.get("SNOWFLAKE_CONNECTION"), schema=schema, query_tag=f"nbo-tests:{schema}")
    s = Snow(conn, schema)
    s.run(f"USE SCHEMA {schema}")
    yield s
    conn.close()


@pytest.fixture(scope="session")
def dates(snow):
    """The retrain date under test and the send that reuses its models (from the real calendar)."""
    d = os.environ.get("NBO_TEST_DATE", "2020-06-01")
    cal = snow.df("SELECT TO_VARCHAR(SEND_DATE, 'YYYY-MM-DD') AS D, RETRAIN, TO_VARCHAR(MODEL_DATE, 'YYYY-MM-DD') AS M "
                  "FROM NBO_SEND_CALENDAR ORDER BY SEND_DATE")
    row = cal[cal["D"] == d]
    assert len(row) and bool(row["RETRAIN"].iloc[0]), f"{d} is not a retrain date in NBO_SEND_CALENDAR"
    reuse = cal[(cal["M"] == d) & (cal["D"] != d)]["D"].tolist()
    return [d] + reuse[:1]


@pytest.fixture(scope="session")
def deployed_version(snow):
    v = snow.df("SELECT CODE_VERSION FROM NBO_DEPLOYMENTS ORDER BY DEPLOYED_AT DESC LIMIT 1")
    assert len(v), "nothing deployed in this schema: run deploy/deploy.py first"
    return v["CODE_VERSION"].iloc[0]


@pytest.fixture(scope="session")
def chain(snow, dates):
    """Runs the send chain in order for each date, exactly like Airflow does. Returns every procedure's answer."""
    n = int(os.environ.get("NBO_TEST_CUSTOMERS", "500"))
    out = {}
    for d in dates:
        out[d] = r = {"checks": snow.call("NBO_DATA_CHECKS", d)}
        assert r["checks"]["status"] in ("SUCCESS", "WARN"), r["checks"]
        r["train"] = snow.call("NBO_TRAIN", d)
        assert r["train"]["status"] in ("SUCCESS", "WARN", "SKIPPED"), r["train"]
        r["recommend"] = snow.call("NBO_RECOMMEND", d, n)
        assert r["recommend"]["status"] == "SUCCESS", r["recommend"]
        r["render"] = snow.call("NBO_RENDER_EMAILS", d)
        assert r["render"]["status"] == "SUCCESS", r["render"]
        r["sent"] = snow.call("NBO_EVENTS", "sent", d)
        assert r["sent"]["status"] == "SUCCESS", r["sent"]
    return out
