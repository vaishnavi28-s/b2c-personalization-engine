-- ============================================================
-- ONE-TIME RESET before the first deploy of the new design (run by hand, once, in HM_ML).
-- Drops tables of the OLD design whose columns changed, and old backtest leftovers.
-- KEEPS: the H&M data (HM_ODS), NBO_NO_IMAGE, NBO_IMAGE_LIST, NBO_EMAIL_RECIPIENTS, the stages and images.
-- The new build.sql then creates every table fresh. Nothing here is ever run by the pipeline.
-- ============================================================
USE SCHEMA NBO_REBUILD.HM_ML;

-- old outputs (different columns in the new design)
DROP TABLE IF EXISTS NBO_RECOMMENDATIONS;
DROP TABLE IF EXISTS NBO_RECOMMENDATION_ITEMS;
DROP TABLE IF EXISTS NBO_RUN_LOG;
DROP TABLE IF EXISTS NBO_EMAIL_OUTBOX;
DROP TABLE IF EXISTS NBO_POOL_BY_DATE;
DROP TABLE IF EXISTS NBO_EVAL_RUNS;
DROP TABLE IF EXISTS NBO_EVAL_RESULTS;
DROP TABLE IF EXISTS NBO_EVAL_DETAIL;

-- old backtest / notebook leftovers (not used by the new code)
DROP TABLE IF EXISTS NBO_BT_EMAILS;
DROP TABLE IF EXISTS NBO_BT_MATCHES;
DROP TABLE IF EXISTS NBO_POOL;
DROP TABLE IF EXISTS NBO_POOL_ARTICLES;

-- old procedures (replaced by the new ones)
DROP PROCEDURE IF EXISTS NBO_RECOMMENDER(NUMBER, VARCHAR, FLOAT, NUMBER, NUMBER);
DROP PROCEDURE IF EXISTS NBO_EVALUATE(VARCHAR, NUMBER, BOOLEAN);
DROP PROCEDURE IF EXISTS NBO_SLOT_TEST(VARCHAR, NUMBER);

SHOW TABLES IN SCHEMA NBO_REBUILD.HM_ML;