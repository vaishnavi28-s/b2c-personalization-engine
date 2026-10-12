"""
Post-deploy smoke test: proves the deployed procedures START in Snowflake (their Python modules and packages
resolve) without heavy work. NBO_EVALUATE for a date whose 90 days are not covered by the data answers PENDING
after importing everything. Exit code 1 = the deploy is broken -> the workflow fails (and PROD is not touched).

  python ci/smoke.py --schema HM_ML_DEV
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
from snowflake_conn import connect  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", required=True)
    ap.add_argument("--connection")
    a = ap.parse_args(argv)
    conn = connect(a.connection, schema=a.schema.upper(), query_tag=f"nbo-smoke:{a.schema}")
    try:
        cur = conn.cursor()
        cur.execute(f"USE SCHEMA {a.schema.upper()}")
        cur.execute("CALL NBO_EVALUATE('2020-09-01', 90)")
        answer = json.loads(cur.fetchone()[0])
        cur.execute("""DELETE FROM NBO_RUN_LOG WHERE PROCEDURE_NAME = 'NBO_EVALUATE' AND RUN_DATE = '2020-09-01'
                       AND STATUS = 'PENDING'""")
    finally:
        conn.close()
    print(json.dumps(answer, indent=2))
    if answer.get("status") != "PENDING":
        raise SystemExit(f"Smoke test FAILED in {a.schema}: expected PENDING, got {answer.get('status')}")
    print(f"Smoke test OK: procedures in {a.schema} start and answer.")


if __name__ == "__main__":
    main()
