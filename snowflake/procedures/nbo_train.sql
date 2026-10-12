-- ============================================================
-- PROCEDURE: NBO_TRAIN(RUN_DATE)
-- Step 3 + 4 of a send. On RETRAIN dates (the 1st): model check (train to date-15, test on the next 15 days,
-- with the best sellers of the week before as baseline), gate vs last month, then train + save both models
-- (or keep last month's if the gate fails).
-- Other dates: SKIPPED (the send uses the models of its MODEL_DATE).
-- Writes NBO_MODEL_REGISTRY, NBO_POOL_BY_DATE, models on @NBO_OUTPUT_STAGE/models, NBO_RUN_LOG.
-- Returns JSON: SUCCESS (gate PASS, beats best sellers) | WARN (gate FAIL -> last month's models kept,
--               or a model does not beat the best sellers) | SKIPPED | FAILED
-- ============================================================
CREATE OR REPLACE PROCEDURE NBO_TRAIN(RUN_DATE VARCHAR)
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
    import model_training as mt

    def step():
        cal = mt.calendar_row(run_date)
        if not bool(cal["RETRAIN"]):
            return "SKIPPED", {"note": "no training on this date", "model_date": cal["MODEL_DATE"]}
        row = mt.train_and_save(run_date)
        status = "SUCCESS" if row["GATE_STATUS"] == "PASS" and row["BEATS_BASELINE"] else "WARN"
        return status, {"gate": row["GATE_STATUS"], "reason": row["GATE_REASON"], "model_used": row["MODEL_USED"],
                        "taste_p5": row["TASTE_P5"], "taste_best_sellers_p5": row["TASTE_BASE_P5"],
                        "basket_p5": row["BASKET_P5"], "basket_best_sellers_p5": row["BASKET_BASE_P5"],
                        "beats_best_sellers": row["BEATS_BASELINE"], "baseline_note": row["BASELINE_NOTE"],
                        "minutes": row["MINUTES"]}

    return ops.run_logged(session, "NBO_TRAIN", run_date, step)
$$;