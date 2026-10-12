-- ============================================================
-- EVENT + EVALUATION TABLES
--   NBO_EMAIL_EVENTS     one row per event: 'sent' (after the send) and 'buy' (from the purchases, by the evaluation)
--                        (a real company's website tracking would add 'open' / 'click' rows here)
--   NBO_EVAL_CUSTOMERS   per evaluated send, one row per customer (emailed or holdout): bought anything, spend,
--                        bought a pick - the pooled holdout comparison over all sends reads it
--   NBO_EVAL_RESULTS     scores per send: by slot, by position, by email, holdout comparison (with 95% ranges)
-- ============================================================

CREATE TABLE IF NOT EXISTS NBO_EMAIL_EVENTS (
    EVENT_ID      VARCHAR,
    EMAIL_ID      VARCHAR,
    RUN_DATE      VARCHAR,
    CUSTOMER_ID   VARCHAR,
    GRP           VARCHAR,           -- EMAIL | HOLDOUT (holdout only gets 'buy' rows: what we would have shown)
    SLOT          NUMBER,            -- slot logic 1-6
    POSITION      NUMBER,            -- where it was shown 1-6
    ARTICLE_ID    VARCHAR,
    PRODUCT_CODE  NUMBER,
    EVENT_TYPE    VARCHAR,           -- sent | buy   (open | click with website tracking)
    EVENT_TIME    VARCHAR,
    SOURCE        VARCHAR,           -- send | orders
    DETAIL        VARCHAR            -- buy: item (exact article) or product (same product, other colour)
);

CREATE TABLE IF NOT EXISTS NBO_EVAL_RESULTS (
    RUN_DATE    VARCHAR,
    GROUP_BY    VARCHAR,             -- slot | position | email | holdout (this send) | holdout_pooled (all sends so far)
    GROUP_KEY   VARCHAR,             -- 1-6, all, email_minus_holdout, holdout_level, min_detectable_lift
    METRIC      VARCHAR,             -- buy_rate, buy_item_rate, email_buy_rate, buys_per_email, any_buy_rate ...
    VALUE       FLOAT,
    CI_LOW      FLOAT,               -- 95% range
    CI_HIGH     FLOAT,
    N           NUMBER
);

CREATE TABLE IF NOT EXISTS NBO_EVAL_CUSTOMERS (
    RUN_DATE     VARCHAR,
    CUSTOMER_ID  VARCHAR,
    GRP          VARCHAR,            -- EMAIL | HOLDOUT
    ANY_BUY      FLOAT,              -- 1 = bought anything within 90 days
    SPEND        FLOAT,              -- money spent within 90 days
    PICK_BUY     FLOAT               -- 1 = bought one of her picks (holdout: what we would have shown)
);