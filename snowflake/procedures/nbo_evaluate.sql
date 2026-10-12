-- ============================================================
-- PROCEDURE: NBO_EVALUATE(RUN_DATE, HORIZON_DAYS)
-- Scores one send once its 90 days are over: buys matched from the purchases (the customer bought a pick -
-- the exact item or the same product in another colour); buy rate per slot, per position, per email;
-- emailed vs holdout for this send and pooled over all sends evaluated so far (with the minimum detectable lift).
-- PENDING while the 90 days are not over.
-- Writes NBO_EMAIL_EVENTS (sent / buy), NBO_EVAL_CUSTOMERS, NBO_EVAL_RESULTS, NBO_RUN_LOG.
-- ============================================================
CREATE OR REPLACE PROCEDURE NBO_EVALUATE(RUN_DATE VARCHAR, HORIZON_DAYS NUMBER DEFAULT 90)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.10'
PACKAGES = ('snowflake-snowpark-python', 'pandas', 'numpy', 'scipy', 'lightfm')
IMPORTS = ('@NBO_OUTPUT_STAGE/model_training.py', '@NBO_OUTPUT_STAGE/ops.py', '@NBO_OUTPUT_STAGE/evaluation.py')
HANDLER = 'run'
EXECUTE AS CALLER
AS
$$
def run(session, run_date, horizon_days=90):
    import ops
    import evaluation as ev

    def step():
        status, rows, info = ev.evaluate(run_date, int(horizon_days))
        return status, info

    return ops.run_logged(session, "NBO_EVALUATE", run_date, step)
$$;