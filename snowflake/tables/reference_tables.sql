-- ============================================================
-- REFERENCE TABLES (filled by other processes, never replaced by a deploy)
--   NBO_NO_IMAGE           articles without a product photo - never shown (an email card needs a photo)
--   NBO_EMAIL_RECIPIENTS   who receives the real test emails; everyone else is a dry run
-- ============================================================

CREATE TABLE IF NOT EXISTS NBO_NO_IMAGE (
    ARTICLE_ID   VARCHAR,            -- 10 digits with leading zero, e.g. 0108775015
    FOUND_ON     VARCHAR,
    NOTE         VARCHAR
);

CREATE TABLE IF NOT EXISTS NBO_EMAIL_RECIPIENTS (
    RECIPIENT_ID      NUMBER AUTOINCREMENT,
    RECIPIENT_EMAIL   VARCHAR,
    RECIPIENT_NAME    VARCHAR,
    DEMO_CUSTOMER_ID  VARCHAR,       -- whose 6 picks this recipient sees; empty = assigned automatically
    ACTIVE            BOOLEAN DEFAULT TRUE
);