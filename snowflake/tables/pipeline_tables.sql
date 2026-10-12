-- ============================================================
-- PIPELINE TABLES (written by the procedures; never replaced by a deploy)
--   NBO_DATA_CHECKS        data checks before a send (freshness, volume, empty keys)
--   NBO_MODEL_REGISTRY     every training: model check vs best sellers, gate PASS/FAIL, which models sends use
--   NBO_POOL_BY_DATE       the shelf of each send date (audit trail)
--   NBO_RECOMMENDATIONS    one row per customer per send (group, profile, the 6 items)
--   NBO_RECOMMENDATION_ITEMS one row per card (slot logic POS, shown POSITION, item, source, rule, score)
--   NBO_RUN_METRICS        quality numbers per send (fill / fallback rates, cap, ...) - read by monitoring
--   NBO_RUN_LOG            one row per procedure run (status, duration, error)
--   NBO_EMAIL_OUTBOX       rendered emails: READY -> SENT / FAILED, or DRY_RUN for customers not really emailed
--   NBO_DEPLOYMENTS        every deploy of this schema: git commit, who, when (deploy.py writes it)
--   NBO_SETTINGS           per-schema overrides of model settings - EMPTY in DEV and PROD (only the CI test schema)
-- ============================================================

CREATE TABLE IF NOT EXISTS NBO_DATA_CHECKS (
    RUN_DATE     VARCHAR,
    CHECK_NAME   VARCHAR,
    VALUE        FLOAT,
    EXPECTED     VARCHAR,
    STATUS       VARCHAR,            -- PASS | WARN | FAIL
    DETAIL       VARCHAR,
    CHECKED_AT   VARCHAR
);

CREATE TABLE IF NOT EXISTS NBO_MODEL_REGISTRY (
    MODEL_DATE              VARCHAR,
    TRAINED_AT              VARCHAR,
    GATE_STATUS             VARCHAR, -- PASS | FAIL
    GATE_REASON             VARCHAR,
    CHECK_TRAINED_TO        VARCHAR, -- check models trained up to this date ...
    TASTE_P5                FLOAT,   -- ... and measured on the 15 days after it
    TASTE_CHECK_CUSTOMERS   NUMBER,
    BASKET_P5               FLOAT,
    BASKET_CHECK_CUSTOMERS  NUMBER,
    PREV_TASTE_P5           FLOAT,   -- last month's numbers (the gate compares with them)
    PREV_BASKET_P5          FLOAT,
    MODEL_USED              VARCHAR, -- the models sends must use (this date, or last month's if the gate failed)
    TASTE_CUSTOMERS         NUMBER,
    TASTE_ITEMS             NUMBER,
    BASKET_CUSTOMERS        NUMBER,
    BASKET_ITEMS            NUMBER,
    MINUTES                 FLOAT,
    CODE_VERSION            VARCHAR, -- git commit of the code that trained the models
    TASTE_BASE_P5           FLOAT,   -- best sellers of the week before, scored on the same customers and buys
    BASKET_BASE_P5          FLOAT,
    BEATS_BASELINE          BOOLEAN, -- both models beat the best sellers (FALSE -> training WARN + alert)
    BASELINE_NOTE           VARCHAR  -- which model lost, by how much
);

CREATE TABLE IF NOT EXISTS NBO_POOL_BY_DATE (
    POOL_DATE          VARCHAR,
    PRODUCT_CODE       NUMBER,
    BEST_ARTICLE_ID    VARCHAR,
    PROD_NAME          VARCHAR,
    PRODUCT_TYPE_NAME  VARCHAR,
    INDEX_GROUP_NAME   VARCHAR,
    SALES_28D          FLOAT,
    MOMENTUM           FLOAT,
    SEASON_INDEX       FLOAT,
    POOL_SCORE         FLOAT
);

CREATE TABLE IF NOT EXISTS NBO_RECOMMENDATIONS (
    RUN_DATE           VARCHAR,
    EMAIL_ID           VARCHAR,
    CUSTOMER_ID        VARCHAR,
    GRP                VARCHAR,      -- EMAIL | HOLDOUT
    AGE_GROUP          VARCHAR,
    SECTION            VARCHAR,      -- Women / Men / Young (for slot 4)
    SECTION_DEFAULTED  BOOLEAN,      -- TRUE = never bought adult clothes, Women assumed
    N_HISTORY          NUMBER,
    INSP_MODE          VARCHAR,      -- AND | OR (slots 5/6 start)
    IN_BASKET_MODEL    BOOLEAN,
    OCCASION           VARCHAR,
    MODEL_DATE         VARCHAR,
    SLOT_1_ARTICLE     VARCHAR,
    SLOT_2_ARTICLE     VARCHAR,
    SLOT_3_ARTICLE     VARCHAR,
    SLOT_4_ARTICLE     VARCHAR,
    SLOT_5_ARTICLE     VARCHAR,
    SLOT_6_ARTICLE     VARCHAR
);

CREATE TABLE IF NOT EXISTS NBO_RECOMMENDATION_ITEMS (
    RUN_DATE      VARCHAR,
    EMAIL_ID      VARCHAR,
    CUSTOMER_ID   VARCHAR,
    GRP           VARCHAR,
    POS           NUMBER,            -- slot logic 1-6
    POSITION      NUMBER,            -- where it is shown in the email 1-6 (shuffled)
    SLOT          VARCHAR,           -- slot name shown in the email
    OCCASION      VARCHAR,
    MODEL_DATE    VARCHAR,
    ARTICLE_ID    VARCHAR,
    PRODUCT_CODE  NUMBER,
    PROD_NAME     VARCHAR,
    DEPARTMENT    VARCHAR,
    SECTION       VARCHAR,
    TYPE          VARCHAR,
    GROUP_NAME    VARCHAR,
    COLOUR        VARCHAR,
    PATTERN       VARCHAR,
    SOURCE        VARCHAR,           -- model | fallback
    RULE          VARCHAR,           -- e.g. AND / OR / best / list 30s/Women / festival
    SCORE         FLOAT
);

CREATE TABLE IF NOT EXISTS NBO_RUN_METRICS (
    RUN_DATE   VARCHAR,
    METRIC     VARCHAR,
    VALUE      FLOAT
);

CREATE TABLE IF NOT EXISTS NBO_RUN_LOG (
    RUN_ID            VARCHAR,
    PROCEDURE_NAME    VARCHAR,       -- NBO_DATA_CHECKS | NBO_TRAIN | NBO_RECOMMEND | NBO_RENDER_EMAILS | NBO_EVENTS | NBO_EVALUATE
    RUN_DATE          VARCHAR,
    START_TIME        VARCHAR,
    END_TIME          VARCHAR,
    DURATION_SECONDS  NUMBER,
    STATUS            VARCHAR,       -- SUCCESS | WARN | FAILED | PENDING | SKIPPED
    DETAIL            VARCHAR,       -- short summary (counts, gate, occasion, model used)
    ERROR_MESSAGE     VARCHAR,
    RUN_BY            VARCHAR,
    CODE_VERSION      VARCHAR        -- git commit deployed when the step ran
);

CREATE TABLE IF NOT EXISTS NBO_EMAIL_OUTBOX (
    RUN_DATE          VARCHAR,
    EMAIL_ID          VARCHAR,
    RECIPIENT_EMAIL   VARCHAR,
    CUSTOMER_ID       VARCHAR,
    SUBJECT           VARCHAR,
    HTML              VARCHAR,
    STATUS            VARCHAR,       -- READY -> SENT | FAILED ; DRY_RUN = recorded, not really sent
    CREATED_AT        VARCHAR,
    SENT_AT           VARCHAR,
    ERROR_MESSAGE     VARCHAR
);

CREATE TABLE IF NOT EXISTS NBO_DEPLOYMENTS (
    CODE_VERSION   VARCHAR,          -- git commit (short hash)
    DEPLOYED_AT    TIMESTAMP_NTZ,
    DEPLOYED_BY    VARCHAR,          -- user / CI run that deployed
    SOURCE         VARCHAR           -- e.g. github-actions run 1234, or laptop
);

CREATE TABLE IF NOT EXISTS NBO_SETTINGS (
    NAME    VARCHAR,                 -- TASTE_EPOCHS | BASKET_EPOCHS | EVAL_USERS | POOL_SIZE
    VALUE   NUMBER,
    REASON  VARCHAR
);

-- tables created before a column existed get it here (no-op when it is already there)
ALTER TABLE NBO_RUN_LOG        ADD COLUMN IF NOT EXISTS CODE_VERSION VARCHAR;
ALTER TABLE NBO_MODEL_REGISTRY ADD COLUMN IF NOT EXISTS CODE_VERSION VARCHAR;
ALTER TABLE NBO_MODEL_REGISTRY ADD COLUMN IF NOT EXISTS TASTE_BASE_P5 FLOAT;
ALTER TABLE NBO_MODEL_REGISTRY ADD COLUMN IF NOT EXISTS BASKET_BASE_P5 FLOAT;
ALTER TABLE NBO_MODEL_REGISTRY ADD COLUMN IF NOT EXISTS BEATS_BASELINE BOOLEAN;
ALTER TABLE NBO_MODEL_REGISTRY ADD COLUMN IF NOT EXISTS BASELINE_NOTE VARCHAR;