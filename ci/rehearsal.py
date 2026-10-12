"""
Monthly DEV rehearsal helpers (used by .github/workflows/rehearsal.yml).

  python ci/rehearsal.py next                        -> prints the next retrain date PRODUCTION has not trained yet
  python ci/rehearsal.py record --date D --status S  -> writes the result into HM_ML_DEV.NBO_RUN_LOG
                                                        (PROCEDURE_NAME = 'CI_REHEARSAL'); the Airflow send DAG
                                                        waits for a SUCCESS row before production trains on D
"""
import argparse
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
from snowflake_conn import connect  # noqa: E402

PROD, DEV = "HM_ML", "HM_ML_DEV"


def next_retrain_date(cur):
    cur.execute(f"""
        SELECT TO_VARCHAR(MIN(c.SEND_DATE), 'YYYY-MM-DD')
        FROM {PROD}.NBO_SEND_CALENDAR c
        WHERE c.RETRAIN
          AND c.SEND_DATE > COALESCE((SELECT MAX(TRY_TO_DATE(MODEL_DATE)) FROM {PROD}.NBO_MODEL_REGISTRY),
                                     '1900-01-01'::DATE)""")
    row = cur.fetchone()
    return row[0] if row else None


def record(cur, date, status, detail):
    cur.execute(f"""
        INSERT INTO {DEV}.NBO_RUN_LOG (RUN_ID, PROCEDURE_NAME, RUN_DATE, START_TIME, END_TIME, DURATION_SECONDS,
                                      STATUS, DETAIL, ERROR_MESSAGE, RUN_BY, CODE_VERSION)
        SELECT %s, 'CI_REHEARSAL', %s, CURRENT_TIMESTAMP()::VARCHAR, CURRENT_TIMESTAMP()::VARCHAR, 0, %s, %s,
               %s, CURRENT_USER(), %s""",
                (f"CI_REHEARSAL_{date}_{uuid.uuid4().hex[:8]}", date, status, detail,
                 "" if status == "SUCCESS" else "integration tests failed - see the GitHub Actions run",
                 os.environ.get("GITHUB_SHA", "unknown")[:12]))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["next", "record"])
    ap.add_argument("--date")
    ap.add_argument("--status", choices=["SUCCESS", "FAILED"])
    ap.add_argument("--connection")
    a = ap.parse_args(argv)
    conn = connect(a.connection, query_tag="nbo-rehearsal")
    try:
        cur = conn.cursor()
        if a.action == "next":
            print(next_retrain_date(cur) or "")
        else:
            if not (a.date and a.status):
                raise SystemExit("record needs --date and --status")
            run = f"{os.environ.get('GITHUB_SERVER_URL', '')}/{os.environ.get('GITHUB_REPOSITORY', '')}" \
                  f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
            record(cur, a.date, a.status, f'{{"github_run": "{run}"}}')
            print(f"recorded CI_REHEARSAL {a.date} {a.status} in {DEV}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
