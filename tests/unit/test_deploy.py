"""
The deploy manifest and its safety rules. A forgotten file or a module a procedure imports but the deploy
does not upload would only show up as a broken procedure in Snowflake - these tests catch it on the pull request.
"""
import glob
import io
import os

import pytest
from snowflake.connector.util_text import split_statements

import deploy

SF = os.path.join(deploy.ROOT, "snowflake")


def test_every_file_in_the_manifest_exists():
    for f in deploy.STAGE_FILES + deploy.TABLE_FILES + deploy.PROCEDURE_FILES:
        assert os.path.exists(os.path.join(SF, f)), f
    for m in deploy.PYTHON_MODULES:
        assert os.path.exists(os.path.join(SF, "python", m)), m


def test_no_procedure_or_table_file_is_forgotten():
    on_disk = {os.path.relpath(p, SF).replace(os.sep, "/") for p in
               glob.glob(os.path.join(SF, "procedures", "*.sql")) + glob.glob(os.path.join(SF, "tables", "*.sql"))}
    assert on_disk == set(deploy.PROCEDURE_FILES + deploy.TABLE_FILES)


def test_every_imported_module_is_uploaded():
    assert deploy.imported_modules() <= set(deploy.PYTHON_MODULES)


def test_every_procedure_imports_model_training_and_ops():
    for f in deploy.PROCEDURE_FILES:
        text = open(os.path.join(SF, f)).read()
        assert "@NBO_OUTPUT_STAGE/model_training.py" in text and "@NBO_OUTPUT_STAGE/ops.py" in text, f


def test_sql_files_split_into_statements():
    """Snowflake's own splitter must see each procedure as ONE statement (the $$ body has semicolons)."""
    for f in deploy.PROCEDURE_FILES:
        st = list(split_statements(io.StringIO(open(os.path.join(SF, f)).read()), remove_comments=True))
        assert len(st) == 1, f
    for f in deploy.TABLE_FILES:
        st = list(split_statements(io.StringIO(open(os.path.join(SF, f)).read()), remove_comments=True))
        assert len(st) >= 2, f


def test_expected_objects():
    assert deploy.expected_procedures() == ["NBO_DATA_CHECKS", "NBO_TRAIN", "NBO_RECOMMEND", "NBO_RENDER_EMAILS",
                                            "NBO_EVENTS", "NBO_EVALUATE"]
    tables = deploy.expected_tables()
    for t in ("NBO_RUN_LOG", "NBO_MODEL_REGISTRY", "NBO_DEPLOYMENTS", "NBO_SETTINGS", "NBO_EVAL_RESULTS",
              "NBO_EVAL_CUSTOMERS", "NBO_SEND_CALENDAR"):
        assert t in tables


def test_tables_files_never_drop_data_tables():
    """A deploy may rebuild config tables, but must never wipe data the pipeline wrote."""
    for f in ["tables/pipeline_tables.sql", "tables/event_tables.sql", "tables/reference_tables.sql"]:
        text = open(os.path.join(SF, f)).read().upper()
        assert "CREATE OR REPLACE" not in text and "DROP " not in text and "TRUNCATE" not in text, f


def test_prod_needs_explicit_permission():
    with pytest.raises(SystemExit, match="PRODUCTION"):
        deploy.check_schema_name("HM_ML", allow_prod=False)
    deploy.check_schema_name("HM_ML", allow_prod=True)
    deploy.check_schema_name("HM_ML_DEV", allow_prod=False)


def test_bad_schema_names_are_refused():
    for bad in ("hm_ml; drop", "HM-ML", "", "1ABC"):
        with pytest.raises(SystemExit):
            deploy.check_schema_name(bad, allow_prod=False)


def test_only_temporary_ci_schemas_can_be_dropped():
    class NoConn:
        def cursor(self):
            raise AssertionError("must not reach Snowflake")
    for schema in ("HM_ML", "HM_ML_DEV", "HM_ODS", "SOMETHING_ELSE"):
        with pytest.raises(SystemExit, match="Refusing"):
            deploy.drop(NoConn(), schema)


def test_setting_overrides_never_reach_dev_or_prod(monkeypatch):
    monkeypatch.setattr(deploy, "connect", lambda *a, **k: pytest.fail("must not connect"))
    for schema in ("HM_ML_DEV", "HM_ML"):
        with pytest.raises(SystemExit, match="only for temporary CI schemas"):
            deploy.main(["deploy", "--schema", schema, "--allow-prod", "--setting", "TASTE_EPOCHS=5"])


def test_parse_settings():
    assert deploy.parse_settings(["taste_epochs=10", "EVAL_USERS = 500"]) == {"TASTE_EPOCHS": 10, "EVAL_USERS": 500}
    with pytest.raises(SystemExit):
        deploy.parse_settings(["TASTE_EPOCHS=ten"])
