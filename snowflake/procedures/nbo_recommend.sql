-- ============================================================
-- PROCEDURE: NBO_RECOMMEND(RUN_DATE, N_CUSTOMERS)
-- Step 5 + 6 of a send: picks the audience (consent, frequency cap, 5% holdout), fills the 6 slots with the
-- models from the registry, stores the picks, the quality numbers and runs the hard output checks.
-- Writes NBO_RECOMMENDATIONS, NBO_RECOMMENDATION_ITEMS, NBO_RUN_METRICS, NBO_POOL_BY_DATE, NBO_RUN_LOG.
-- Returns JSON: SUCCESS | FAILED (a hard check failed - picks are stored for debugging but must not be sent)
-- ============================================================
CREATE OR REPLACE PROCEDURE NBO_RECOMMEND(RUN_DATE VARCHAR, N_CUSTOMERS NUMBER DEFAULT 5000)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.10'
PACKAGES = ('snowflake-snowpark-python', 'pandas', 'numpy', 'scipy', 'lightfm')
IMPORTS = ('@NBO_OUTPUT_STAGE/model_training.py', '@NBO_OUTPUT_STAGE/inference.py', '@NBO_OUTPUT_STAGE/ops.py')
HANDLER = 'run'
EXECUTE AS CALLER
AS
$$
def run(session, run_date, n_customers=5000):
    import ops
    import model_training as mt
    import inference as inf

    def step():
        taste, basket, used = mt.models_for(run_date)
        wide, items, metrics, failed, info = inf.run_send(run_date, taste, basket, int(n_customers))
        ops.save_send(run_date, wide, items)
        ops.save_metrics(run_date, metrics)
        bad = inf.hard_check(metrics)
        detail = {"models_used": used, "occasion": info["occasion"], "occasion_source": info["occasion_source"],
                  "customers": metrics.get("customers"), "emails": metrics.get("customers_email"),
                  "holdout": metrics.get("customers_holdout"), "failed_checks": bad, "first_errors": failed}
        return ("FAILED" if bad else "SUCCESS"), detail

    return ops.run_logged(session, "NBO_RECOMMEND", run_date, step)
$$;