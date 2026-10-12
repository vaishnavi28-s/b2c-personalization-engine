"""
Makes the project code importable for every test folder:
  tests/unit         pure Python: rules, evaluation, email, settings, deploy manifest, monitoring config (every PR)
  tests/dags         Airflow DAG checks, run inside the Airflow image (every PR)
  tests/integration  the real pipeline on real Snowflake data, in a temporary schema (after merge / monthly)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("snowflake/python", "deploy", "airflow/dags"):
    path = os.path.join(ROOT, sub)
    if path not in sys.path:
        sys.path.insert(0, path)
