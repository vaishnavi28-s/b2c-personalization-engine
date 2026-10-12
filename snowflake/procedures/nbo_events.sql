-- ============================================================
-- PROCEDURE: NBO_EVENTS(MODE, RUN_DATE)
--   MODE 'sent' : after Airflow sent the emails, write one 'sent' event per card of the date's emails
--                 into NBO_EMAIL_EVENTS (re-run safe). The evaluation later adds the 'buy' events.
-- (In a real company the website's tracking would add 'open' / 'click' events to the same table.)
-- Writes NBO_RUN_LOG.
-- ============================================================
CREATE OR REPLACE PROCEDURE NBO_EVENTS(MODE VARCHAR, RUN_DATE VARCHAR DEFAULT NULL)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.10'
PACKAGES = ('snowflake-snowpark-python', 'pandas', 'numpy', 'scipy', 'lightfm')
IMPORTS = ('@NBO_OUTPUT_STAGE/model_training.py', '@NBO_OUTPUT_STAGE/ops.py', '@NBO_OUTPUT_STAGE/evaluation.py')
HANDLER = 'run'
EXECUTE AS CALLER
AS
$$
def run(session, mode, run_date=None):
    import ops
    import evaluation as ev

    def step():
        if str(mode).lower() == "sent":
            return "SUCCESS", {"sent_cards": ev.record_sent(run_date)}
        raise ValueError(f"unknown MODE {mode}: use sent")

    return ops.run_logged(session, f"NBO_EVENTS_{str(mode).upper()}", run_date, step)
$$;