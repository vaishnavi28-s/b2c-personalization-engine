-- ============================================================
-- CONFIG TABLES (the files are the source of truth: edit here, re-run build.sql)
--   NBO_SEND_CALENDAR   the schedule: one row per send date, which models it uses, its occasion
--   NBO_EVENT_CALENDAR  festival dates 2018-2020 and how long before them people shop
--   NBO_MONTH_THEMES    month themes found in the data (built only from data before the first send)
--   NBO_SEASON_RULES    what may never be shown in which months
-- No database / schema prefix: build.sql chooses the schema (HM_ML_DEV or HM_ML).
-- ============================================================

CREATE OR REPLACE TABLE NBO_SEND_CALENDAR (
    SEND_DATE    DATE    NOT NULL,
    RETRAIN      BOOLEAN NOT NULL,   -- TRUE on the 1st: both models are trained (with the model check)
    MODEL_DATE   DATE    NOT NULL,   -- which training this send uses (the 16th uses the 1st)
    OCCASION     VARCHAR,            -- name shown in the email
    OCC_TYPE     VARCHAR,            -- event / theme / fallback
    OCC_DATE     DATE,               -- festival date this year
    OCC_DATE_LY  DATE,               -- same festival last year (to see what sold before it)
    OCC_DAYS     NUMBER,             -- how many days before the festival people shop for it
    THEME_COL    VARCHAR,            -- theme: which product column ...
    THEME_VALUE  VARCHAR             -- ... and which value (e.g. PRODUCT_GROUP_NAME = Swimwear)
);

INSERT INTO NBO_SEND_CALENDAR VALUES
('2019-10-01', TRUE,  '2019-10-01', 'Popular right now',        'fallback', NULL,         NULL,         NULL, NULL, NULL),
('2019-10-16', FALSE, '2019-10-01', 'Halloween',                'event',    '2019-10-31', '2018-10-31', 21,   NULL, NULL),
('2019-11-01', TRUE,  '2019-11-01', 'Popular right now',        'fallback', NULL,         NULL,         NULL, NULL, NULL),
('2019-11-16', FALSE, '2019-11-01', 'Popular right now',        'fallback', NULL,         NULL,         NULL, NULL, NULL),
('2019-12-01', TRUE,  '2019-12-01', 'Christmas / party season', 'event',    '2019-12-24', '2018-12-24', 28,   NULL, NULL),
('2019-12-16', FALSE, '2019-12-01', 'Christmas / party season', 'event',    '2019-12-24', '2018-12-24', 28,   NULL, NULL),
('2020-01-01', TRUE,  '2020-01-01', 'Active / sport',           'theme',    NULL,         NULL,         NULL, 'INDEX_GROUP_NAME',   'Sport'),
('2020-01-16', FALSE, '2020-01-01', 'Active / sport',           'theme',    NULL,         NULL,         NULL, 'INDEX_GROUP_NAME',   'Sport'),
('2020-02-01', TRUE,  '2020-02-01', 'Valentine''s Day',         'event',    '2020-02-14', '2019-02-14', 21,   NULL, NULL),
('2020-02-16', FALSE, '2020-02-01', 'Popular right now',        'fallback', NULL,         NULL,         NULL, NULL, NULL),
('2020-03-01', TRUE,  '2020-03-01', 'Popular right now',        'fallback', NULL,         NULL,         NULL, NULL, NULL),
('2020-03-16', FALSE, '2020-03-01', 'Popular right now',        'fallback', NULL,         NULL,         NULL, NULL, NULL),
('2020-04-01', TRUE,  '2020-04-01', 'Easter',                   'event',    '2020-04-12', '2019-04-21', 21,   NULL, NULL),
('2020-04-16', FALSE, '2020-04-01', 'Popular right now',        'fallback', NULL,         NULL,         NULL, NULL, NULL),
('2020-05-01', TRUE,  '2020-05-01', 'Mother''s Day',            'event',    '2020-05-10', '2019-05-12', 14,   NULL, NULL),
('2020-05-16', FALSE, '2020-05-01', 'Summer vacation',          'theme',    NULL,         NULL,         NULL, 'PRODUCT_GROUP_NAME', 'Swimwear'),
('2020-06-01', TRUE,  '2020-06-01', 'Summer vacation',          'theme',    NULL,         NULL,         NULL, 'PRODUCT_GROUP_NAME', 'Swimwear'),
('2020-06-16', FALSE, '2020-06-01', 'Summer vacation',          'theme',    NULL,         NULL,         NULL, 'PRODUCT_GROUP_NAME', 'Swimwear'),
('2020-07-01', TRUE,  '2020-07-01', 'Summer vacation',          'theme',    NULL,         NULL,         NULL, 'PRODUCT_GROUP_NAME', 'Swimwear'),
('2020-07-16', FALSE, '2020-07-01', 'Summer vacation',          'theme',    NULL,         NULL,         NULL, 'PRODUCT_GROUP_NAME', 'Swimwear'),
('2020-08-01', TRUE,  '2020-08-01', 'Popular right now',        'fallback', NULL,         NULL,         NULL, NULL, NULL),
('2020-08-16', FALSE, '2020-08-01', 'Popular right now',        'fallback', NULL,         NULL,         NULL, NULL, NULL),
('2020-09-01', TRUE,  '2020-09-01', 'Active / sport',           'theme',    NULL,         NULL,         NULL, 'INDEX_GROUP_NAME',   'Sport');


CREATE OR REPLACE TABLE NBO_EVENT_CALENDAR (
    EVENT_NAME   VARCHAR NOT NULL,
    EVENT_DATE   DATE    NOT NULL,
    DAYS_BEFORE  NUMBER  NOT NULL,   -- how long before the festival people shop for it
    SOURCE       VARCHAR,            -- holidays_lib = from a holiday calendar | date_rule = fixed rule (e.g. 14 Feb)
    PUSH_FROM    DATE                -- EVENT_DATE - DAYS_BEFORE
);

INSERT INTO NBO_EVENT_CALENDAR VALUES
('New year',                 '2018-01-01', 14, 'holidays_lib', '2017-12-18'),
('Valentine''s Day',         '2018-02-14', 21, 'date_rule',    '2018-01-24'),
('Easter',                   '2018-04-01', 21, 'holidays_lib', '2018-03-11'),
('Mother''s Day',            '2018-05-13', 14, 'date_rule',    '2018-04-29'),
('Halloween',                '2018-10-31', 21, 'date_rule',    '2018-10-10'),
('Christmas / party season', '2018-12-24', 28, 'holidays_lib', '2018-11-26'),
('New year',                 '2019-01-01', 14, 'holidays_lib', '2018-12-18'),
('Valentine''s Day',         '2019-02-14', 21, 'date_rule',    '2019-01-24'),
('Easter',                   '2019-04-21', 21, 'holidays_lib', '2019-03-31'),
('Mother''s Day',            '2019-05-12', 14, 'date_rule',    '2019-04-28'),
('Halloween',                '2019-10-31', 21, 'date_rule',    '2019-10-10'),
('Christmas / party season', '2019-12-24', 28, 'holidays_lib', '2019-11-26'),
('New year',                 '2020-01-01', 14, 'holidays_lib', '2019-12-18'),
('Valentine''s Day',         '2020-02-14', 21, 'date_rule',    '2020-01-24'),
('Easter',                   '2020-04-12', 21, 'holidays_lib', '2020-03-22'),
('Mother''s Day',            '2020-05-10', 14, 'date_rule',    '2020-04-26'),
('Halloween',                '2020-10-31', 21, 'date_rule',    '2020-10-10'),
('Christmas / party season', '2020-12-24', 28, 'holidays_lib', '2020-11-26');


CREATE OR REPLACE TABLE NBO_MONTH_THEMES (
    M            NUMBER  NOT NULL,   -- month 1-12
    THEME        VARCHAR NOT NULL,
    THEME_COL    VARCHAR NOT NULL,
    THEME_VALUE  VARCHAR NOT NULL,
    THEME_INDEX  FLOAT,              -- e.g. 1.80 = sells 1.8x a normal month
    SOURCE       VARCHAR,
    BUILT_FROM   DATE,
    BUILT_TO     DATE
);

INSERT INTO NBO_MONTH_THEMES VALUES
(1, 'Active / sport',  'INDEX_GROUP_NAME',   'Sport',    1.4315, 'data_theme', '2018-10-01', '2019-10-01'),
(5, 'Summer vacation', 'PRODUCT_GROUP_NAME', 'Swimwear', 1.6938, 'data_theme', '2018-10-01', '2019-10-01'),
(6, 'Summer vacation', 'PRODUCT_GROUP_NAME', 'Swimwear', 1.8042, 'data_theme', '2018-10-01', '2019-10-01'),
(7, 'Summer vacation', 'PRODUCT_GROUP_NAME', 'Swimwear', 1.6845, 'data_theme', '2018-10-01', '2019-10-01'),
(9, 'Active / sport',  'INDEX_GROUP_NAME',   'Sport',    1.2090, 'data_theme', '2018-10-01', '2019-10-01');


CREATE OR REPLACE TABLE NBO_SEASON_RULES (
    PRODUCT_GROUP   VARCHAR,
    PRODUCT_TYPE    VARCHAR,         -- empty = the whole group
    BLOCKED_MONTHS  VARCHAR,         -- month numbers, comma separated
    REASON          VARCHAR
);

INSERT INTO NBO_SEASON_RULES VALUES
('Swimwear',           NULL,     '9,10,11,12,1,2,3,4', 'no swimwear outside summer'),
('Garment Lower body', 'Shorts', '10,11,12,1,2,3',     'no shorts in cold months'),
('Garment Upper body', 'Coat',   '5,6,7,8',            'no coats in summer');