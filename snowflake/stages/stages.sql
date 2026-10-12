-- ============================================================
-- STAGES (file storage inside Snowflake)
--   NBO_OUTPUT_STAGE  the Python files the procedures import + the trained models (models/taste_<date>.pkl ...)
--   NBO_IMAGES        product photos: lives only in HM_ML (created when the photos were uploaded), shared by DEV
-- ============================================================
CREATE STAGE IF NOT EXISTS NBO_OUTPUT_STAGE
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    COMMENT = 'Python modules imported by the NBO procedures + trained models';