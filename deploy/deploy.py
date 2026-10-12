"""
Deploys the recommender into ONE Snowflake schema. The only way code reaches Snowflake: GitHub Actions runs it,
and you can run the same command on your laptop for DEV.

  python deploy/deploy.py deploy --schema HM_ML_DEV --connection nbo_rebuild          (laptop, DEV)
  python deploy/deploy.py deploy --schema HM_ML --allow-prod                          (CI only, after approval)
  python deploy/deploy.py deploy --schema HM_ML_CI_123 --reference-from HM_ML \\
        --setting TASTE_EPOCHS=10 --setting BASKET_EPOCHS=10                          (CI test schema)
  python deploy/deploy.py drop --schema HM_ML_CI_123                                  (CI clean-up)

What a deploy does, in order (stops at the first error, exit code 1):
  1. create / use the schema, set the Python package channel
  2. stage, then upload the 5 Python modules the procedures import
  3. tables (config tables are rebuilt from their files, data tables are kept)
  4. the 6 procedures
  5. optional: copy reference tables from another schema (zero-copy clone), write setting overrides
  6. record the deploy (git commit) in NBO_DEPLOYMENTS
  7. verify: all procedures, modules and tables exist - otherwise the deploy FAILS
Safe to run again.
"""
import argparse
import io
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from snowflake_conn import connect  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SF = ROOT / "snowflake"
PROD_SCHEMAS = {"HM_ML"}
PROTECTED = {"HM_ML", "HM_ML_DEV", "HM_ODS"}            # never dropped by this script
CI_PREFIX = "HM_ML_CI_"

STAGE_FILES = ["stages/stages.sql"]
PYTHON_MODULES = ["model_training.py", "inference.py", "evaluation.py", "emails.py", "ops.py"]
TABLE_FILES = ["tables/config_tables.sql", "tables/reference_tables.sql", "tables/pipeline_tables.sql",
               "tables/event_tables.sql"]
PROCEDURE_FILES = ["procedures/nbo_data_checks.sql", "procedures/nbo_train.sql", "procedures/nbo_recommend.sql",
                   "procedures/nbo_render_emails.sql", "procedures/nbo_events.sql", "procedures/nbo_evaluate.sql"]
REFERENCE_TABLES = ["NBO_NO_IMAGE", "NBO_EMAIL_RECIPIENTS"]


# ---------------- what the deploy expects to exist afterwards ----------------
def expected_procedures():
    names = []
    for f in PROCEDURE_FILES:
        m = re.search(r"CREATE OR REPLACE PROCEDURE\s+(\w+)", (SF / f).read_text(), re.I)
        names.append(m.group(1).upper())
    return names


def expected_tables():
    names = []
    for f in TABLE_FILES:
        names += [n.upper() for n in re.findall(r"CREATE (?:OR REPLACE )?TABLE (?:IF NOT EXISTS )?(\w+)",
                                                (SF / f).read_text(), re.I)]
    return names


def imported_modules():
    """Every module a procedure imports must be in PYTHON_MODULES (checked by the unit tests too)."""
    mods = set()
    for f in PROCEDURE_FILES:
        mods |= set(re.findall(r"@NBO_OUTPUT_STAGE/(\w+\.py)", (SF / f).read_text()))
    return mods


def git_version():
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha[:12]
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, text=True).strip()
        dirty = subprocess.call(["git", "diff", "--quiet"], cwd=ROOT) != 0
        return sha + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


def check_schema_name(schema, allow_prod):
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", schema):
        raise SystemExit(f"Bad schema name {schema!r}: use capitals, digits and _")
    if schema in PROD_SCHEMAS and not allow_prod:
        raise SystemExit(f"{schema} is PRODUCTION: only the approved CI job deploys there (--allow-prod).")


# ---------------- running SQL ----------------
def run_file(conn, path):
    text = path.read_text()
    n = 0
    for cur in conn.execute_stream(io.StringIO(text), remove_comments=True):
        n += 1
        cur.close()
    print(f"  {path.relative_to(ROOT)}: {n} statements")


def one(conn, sql, params=None):
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        cur.close()


def deploy(conn, schema, version, source, reference_from=None, settings=None):
    print(f"Deploying {version} to {conn.database}.{schema}")
    one(conn, f"CREATE SCHEMA IF NOT EXISTS {schema}")
    one(conn, f"USE SCHEMA {schema}")
    one(conn, f"ALTER SCHEMA {schema} SET DEFAULT_PYTHON_ARTIFACT_REPOSITORY = "
              "snowflake.snowpark.anaconda_shared_repository")

    for f in STAGE_FILES:
        run_file(conn, SF / f)
    for m in PYTHON_MODULES:
        p = (SF / "python" / m).resolve().as_posix()
        one(conn, f"PUT 'file://{p}' @NBO_OUTPUT_STAGE AUTO_COMPRESS = FALSE OVERWRITE = TRUE")
    print(f"  uploaded {len(PYTHON_MODULES)} Python modules")
    for f in TABLE_FILES + PROCEDURE_FILES:
        run_file(conn, SF / f)

    if reference_from:
        have = {r[1].upper() for r in one(conn, f"SHOW TABLES LIKE 'NBO_%' IN SCHEMA {reference_from}")}
        for t in REFERENCE_TABLES:
            if t in have:
                one(conn, f"CREATE OR REPLACE TABLE {schema}.{t} CLONE {reference_from}.{t}")
                print(f"  {t} cloned from {reference_from} (zero-copy)")
            else:
                print(f"  {t} not in {reference_from} yet: left empty")
    if settings:
        one(conn, f"DELETE FROM {schema}.NBO_SETTINGS")
        for name, value in settings.items():
            one(conn, f"INSERT INTO {schema}.NBO_SETTINGS (NAME, VALUE, REASON) VALUES (%s, %s, %s)",
                (name, value, f"set by deploy ({source})"))
        print(f"  settings overrides: {settings}")

    one(conn, f"INSERT INTO {schema}.NBO_DEPLOYMENTS (CODE_VERSION, DEPLOYED_AT, DEPLOYED_BY, SOURCE) "
              "SELECT %s, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ, CURRENT_USER(), %s", (version, source))
    verify(conn, schema)
    print(f"OK: {schema} runs {version}")


def verify(conn, schema):
    problems = []
    procs = {r[1].upper() for r in one(conn, f"SHOW PROCEDURES LIKE 'NBO_%' IN SCHEMA {schema}")}
    problems += [f"procedure {p} missing" for p in expected_procedures() if p not in procs]
    files = {r[0].split("/")[-1] for r in one(conn, f"LIST @{schema}.NBO_OUTPUT_STAGE PATTERN = '.*\\.py'")}
    problems += [f"module {m} missing on the stage" for m in PYTHON_MODULES if m not in files]
    tables = {r[1].upper() for r in one(conn, f"SHOW TABLES LIKE 'NBO_%' IN SCHEMA {schema}")}
    problems += [f"table {t} missing" for t in expected_tables() if t not in tables]
    if problems:
        raise SystemExit("Deploy verification FAILED:\n  " + "\n  ".join(problems))
    print(f"  verified: {len(procs)} procedures, {len(files)} modules, {len(tables)} tables")


def drop(conn, schema):
    if schema in PROTECTED or not schema.startswith(CI_PREFIX):
        raise SystemExit(f"Refusing to drop {schema}: only temporary {CI_PREFIX}* schemas can be dropped.")
    one(conn, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    print(f"dropped {schema}")


def parse_settings(items):
    out = {}
    for s in items or []:
        name, _, value = s.partition("=")
        value = value.strip()
        if not value.lstrip("-").isdigit():
            raise SystemExit(f"--setting {s!r}: use NAME=integer")
        out[name.strip().upper()] = int(value)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["deploy", "drop"])
    ap.add_argument("--schema", required=True)
    ap.add_argument("--connection", help="named connection in ~/.snowflake/connections.toml (laptop)")
    ap.add_argument("--allow-prod", action="store_true", help="needed to deploy to HM_ML (CI after approval)")
    ap.add_argument("--reference-from", help="clone NBO_NO_IMAGE + NBO_EMAIL_RECIPIENTS from this schema")
    ap.add_argument("--setting", action="append", help="NAME=VALUE written to NBO_SETTINGS (CI test schema only)")
    ap.add_argument("--source", default=os.environ.get("NBO_DEPLOY_SOURCE", "laptop"))
    a = ap.parse_args(argv)
    schema = a.schema.upper()
    check_schema_name(schema, a.allow_prod)
    settings = parse_settings(a.setting)
    if settings and schema in PROD_SCHEMAS | {"HM_ML_DEV"}:
        raise SystemExit("Setting overrides are only for temporary CI schemas: DEV and PROD run the real settings.")
    conn = connect(a.connection, query_tag=f"nbo-deploy:{schema}")
    try:
        if a.action == "drop":
            drop(conn, schema)
        else:
            deploy(conn, schema, git_version(), a.source, a.reference_from, settings)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
