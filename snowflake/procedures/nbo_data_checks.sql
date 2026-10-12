-- ============================================================
-- PROCEDURE: NBO_DATA_CHECKS(RUN_DATE)
-- Step 2 of a send: is the input data fit? Fresh purchases, empty ids, unknown articles (FAIL = stop),
-- volume vs the 4 weeks before (WARN = alert, continue). Writes NBO_DATA_CHECKS + NBO_RUN_LOG.
-- Returns JSON: {"status": "SUCCESS" | "WARN" | "FAILED", ...}
-- ============================================================
CREATE OR REPLACE PROCEDURE NBO_DATA_CHECKS(RUN_DATE VARCHAR)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.10'
PACKAGES = ('snowflake-snowpark-python', 'pandas', 'numpy', 'scipy', 'lightfm')
IMPORTS = ('@NBO_OUTPUT_STAGE/model_training.py', '@NBO_OUTPUT_STAGE/ops.py')
HANDLER = 'run'
EXECUTE AS CALLER
AS
$$
def run(session, run_date):
    import ops
    return ops.run_logged(session, "NBO_DATA_CHECKS", run_date, lambda: ops.data_checks(run_date))
$$;