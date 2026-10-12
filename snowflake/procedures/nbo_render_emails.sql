-- ============================================================
-- PROCEDURE: NBO_RENDER_EMAILS(RUN_DATE)
-- Step 8 (part 1) of a send. Only if the latest NBO_RECOMMEND run of the date is SUCCESS:
-- READY HTML emails for the test recipients (6 product cards in their shuffled order), DRY_RUN rows for every
-- emailed customer. Writes NBO_EMAIL_OUTBOX, NBO_RUN_LOG. Airflow sends the READY rows.
-- ============================================================
CREATE OR REPLACE PROCEDURE NBO_RENDER_EMAILS(RUN_DATE VARCHAR)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.10'
PACKAGES = ('snowflake-snowpark-python', 'pandas', 'numpy', 'scipy', 'lightfm')
IMPORTS = ('@NBO_OUTPUT_STAGE/model_training.py', '@NBO_OUTPUT_STAGE/ops.py', '@NBO_OUTPUT_STAGE/emails.py')
HANDLER = 'run'
EXECUTE AS CALLER
AS
$$
def run(session, run_date):
    import ops
    import model_training as mt
    import emails

    def step():
        d = mt.ymd(run_date)
        last = mt.q(f"""SELECT STATUS FROM {mt.DB}.NBO_RUN_LOG
                        WHERE PROCEDURE_NAME = 'NBO_RECOMMEND' AND RUN_DATE = '{d}'
                        ORDER BY START_TIME DESC LIMIT 1""")
        status = last["STATUS"].iloc[0] if len(last) else "NO RUN"
        if status != "SUCCESS":
            return "FAILED", {"blocked": f"latest NBO_RECOMMEND for {d} is {status}"}
        return "SUCCESS", emails.build_outbox(d)

    return ops.run_logged(session, "NBO_RENDER_EMAILS", run_date, step)
$$;