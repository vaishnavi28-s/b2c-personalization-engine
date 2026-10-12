-- ============================================================
-- ONE-TIME SETUP (run by hand as ACCOUNTADMIN): logins for the machines + a cost limit.
-- Two service users, one per system, so you can see in Snowflake which system ran what and switch one off alone:
--   NBO_CI_SVC       GitHub Actions (deploys + tests)
--   NBO_AIRFLOW_SVC  Airflow (the sends, evaluation, export)
-- TYPE = SERVICE: no password, no browser login - only a key pair. The private key lives in GitHub Secrets /
-- the Airflow connection, never in the repo. Role: ACCOUNTADMIN for now (a dedicated role is the next hardening).
--
-- Make one key pair per user on your laptop (Git Bash / WSL), outside the project folder:
--   openssl genrsa 2048 | openssl pkcs8 -topk8 -v2 aes256 -inform PEM -out nbo_ci_key.p8        (asks a passphrase)
--   openssl rsa -in nbo_ci_key.p8 -pubout -out nbo_ci_key.pub
-- Paste the .pub content (without the BEGIN/END lines) below.
-- ============================================================
USE ROLE ACCOUNTADMIN;

CREATE USER IF NOT EXISTS NBO_CI_SVC
    TYPE = SERVICE
    DEFAULT_ROLE = ACCOUNTADMIN
    DEFAULT_WAREHOUSE = NBO_WH
    COMMENT = 'GitHub Actions: deploys and tests the recommender';
ALTER USER NBO_CI_SVC SET RSA_PUBLIC_KEY = '<paste nbo_ci_key.pub here>';
GRANT ROLE ACCOUNTADMIN TO USER NBO_CI_SVC;

CREATE USER IF NOT EXISTS NBO_AIRFLOW_SVC
    TYPE = SERVICE
    DEFAULT_ROLE = ACCOUNTADMIN
    DEFAULT_WAREHOUSE = NBO_WH
    COMMENT = 'Airflow: runs the scheduled pipeline';
ALTER USER NBO_AIRFLOW_SVC SET RSA_PUBLIC_KEY = '<paste nbo_airflow_key.pub here>';
GRANT ROLE ACCOUNTADMIN TO USER NBO_AIRFLOW_SVC;

-- warehouse: stops after 60 s idle, so CI and Airflow only pay while they run
ALTER WAREHOUSE NBO_WH SET AUTO_SUSPEND = 60 AUTO_RESUME = TRUE;

-- cost limit: e-mail at 75% of the monthly credits, stop new queries at 100%
CREATE RESOURCE MONITOR IF NOT EXISTS NBO_MONTHLY_LIMIT
    WITH CREDIT_QUOTA = 30
    FREQUENCY = MONTHLY
    START_TIMESTAMP = IMMEDIATELY
    TRIGGERS ON 75 PERCENT DO NOTIFY
             ON 100 PERCENT DO SUSPEND;
ALTER WAREHOUSE NBO_WH SET RESOURCE_MONITOR = NBO_MONTHLY_LIMIT;

-- check: both users exist with a key
DESC USER NBO_CI_SVC;
DESC USER NBO_AIRFLOW_SVC;