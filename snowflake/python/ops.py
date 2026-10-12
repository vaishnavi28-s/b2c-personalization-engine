from __future__ import annotations

# ======================================================================
# ops.py - what every procedure shares
#   run_logged    run one step, write its NBO_RUN_LOG row (status, duration, detail, error, code version)
#   data_checks   is the input data fit for a send? (schema, freshness, keys, prices, attributes, volume)
#   save_metrics  store a send's quality numbers in NBO_RUN_METRICS (monitoring reads them)
#   save_send     store a send's picks (re-run safe)
# ======================================================================
import json
import time
import traceback

import pandas as pd

import model_training as mt

VOLUME_TOLERANCE    = 0.30       # last 7 days vs the 4 weeks before: more than +/-30% -> WARN
UNKNOWN_MAX         = 0.01       # > 1% of purchases with an article / customer we do not know -> FAIL
BAD_PRICE_MAX       = 0.01       # > 1% of purchases with an empty, zero/negative or absurd price -> FAIL
PRICE_LIMIT         = 1.0        # prices in this dataset are scaled to 0..~0.6; above 1.0 is a broken value
MISSING_ATTR_MAX    = 0.01       # > 1% of the articles sold in 28 days without type/group/colour/pattern -> FAIL
REPEAT_ROWS_SHIFT   = 0.05       # identical rows are normal here (2 units = 2 rows); a jump of > 5 points -> WARN

REQUIRED_COLUMNS = {             # what the models read; a missing column stops the send before anything breaks
    "HM_NBO_DWH_F_TRANSACTION": ["CUSTOMER_ID", "ARTICLE_ID", "T_DAT", "PRICE"],
    "HM_NBO_DWH_D_ARTICLE": ["ARTICLE_ID", "PRODUCT_CODE", "PROD_NAME", "INDEX_GROUP_NAME", "SECTION_NAME",
                             "PRODUCT_TYPE_NAME", "PRODUCT_GROUP_NAME", "PERCEIVED_COLOUR_MASTER_NAME",
                             "GRAPHICAL_APPEARANCE_NAME"],
    "HM_NBO_DWH_D_CUSTOMER": ["CUSTOMER_ID", "AGE", "CLUB_MEMBER_STATUS", "FASHION_NEWS_FREQUENCY"],
}


def _write(df, table):
    db, schema = mt.DB.split(".")
    if len(df):
        mt.session.write_pandas(df, table, database=db, schema=schema, auto_create_table=False)


def run_logged(session, procedure, run_date, step):
    """Runs step() -> (status, detail dict). Always writes one NBO_RUN_LOG row, even on errors.
    Returns a JSON string: {"status": ..., "run_date": ..., ...detail} - Airflow reads it."""
    start, t0 = pd.Timestamp.utcnow(), time.time()
    status, detail, error = "FAILED", {}, ""
    d = ""
    try:
        mt.init(session)                                    # sets mt.DB first, so even a bad setting is logged
        d = mt.ymd(run_date) if run_date else ""
        status, detail = step()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:1500] + " | " + traceback.format_exc()[-400:]
    end = pd.Timestamp.utcnow()
    run_by = session.sql("SELECT CURRENT_USER() AS U").to_pandas()["U"].iloc[0]
    _write(pd.DataFrame([dict(
        RUN_ID=f"{procedure}_{d}_{start.strftime('%Y%m%dT%H%M%S')}", PROCEDURE_NAME=procedure, RUN_DATE=d,
        START_TIME=str(start), END_TIME=str(end), DURATION_SECONDS=int(time.time() - t0), STATUS=status,
        DETAIL=json.dumps(detail, default=str)[:4000], ERROR_MESSAGE=error, RUN_BY=str(run_by),
        CODE_VERSION=mt.code_version())]), "NBO_RUN_LOG")
    return json.dumps({**detail, "status": status, "run_date": d, "procedure": procedure, "error": error[:500]},
                      default=str)


# ---------------- data checks ----------------
def missing_columns():
    """Required source columns that do not exist (schema drift upstream)."""
    db, src = mt.TX.split(".")[0], mt.SOURCE_SCHEMA
    names = ", ".join(f"'{t}'" for t in REQUIRED_COLUMNS)
    have = mt.q(f"""SELECT TABLE_NAME, COLUMN_NAME FROM {db}.INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = '{src}' AND TABLE_NAME IN ({names})""")
    have = set(zip(have["TABLE_NAME"].str.upper(), have["COLUMN_NAME"].str.upper()))
    return [f"{t}.{c}" for t, cols in REQUIRED_COLUMNS.items() for c in cols if (t, c) not in have]


def data_checks(d):
    """Is the input data fit for a send on date d? Writes NBO_DATA_CHECKS. Returns (status, detail).
       FAIL (stop the send):
         required columns missing | no purchases in the last 3 days | empty customer / article ids
         | > 1% unknown articles or customers | > 1% broken prices | purchases dated after today
         | > 1% of recently sold articles without type / group / colour / pattern / department
       WARN (alert, continue - big real changes happen, e.g. the March 2020 lockdown):
         purchase volume of the last 7 days differs > 30% from the 4 weeks before
         | share of repeated identical rows moved > 5 points (in this data 2 units of an item = 2 rows,
           so repeats are normal; a sudden change points to a double load upstream)"""
    d = mt.ymd(d)
    rows = []

    def add(name, value, expected, ok, warn_only=False, detail=""):
        status = "PASS" if ok else ("WARN" if warn_only else "FAIL")
        rows.append(dict(RUN_DATE=d, CHECK_NAME=name, VALUE=float(value), EXPECTED=expected, STATUS=status,
                         DETAIL=str(detail)[:1000], CHECKED_AT=str(pd.Timestamp.utcnow())))

    missing = missing_columns()
    add("required_columns_missing", len(missing), "= 0", not missing, detail=", ".join(missing))
    if not missing:                                         # the other checks would only crash on a broken schema
        r = mt.q(f"""
            SELECT COUNT_IF(T_DAT > DATEADD('day', -3, '{d}') AND T_DAT <= '{d}')  AS LAST_3D,
                   COUNT_IF(T_DAT > DATEADD('day', -7, '{d}') AND T_DAT <= '{d}')  AS LAST_7D,
                   COUNT_IF(T_DAT <= DATEADD('day', -7, '{d}'))                     AS PREV_28D,
                   COUNT_IF(T_DAT > DATEADD('day', -7, '{d}') AND T_DAT <= '{d}'
                            AND (CUSTOMER_ID IS NULL OR ARTICLE_ID IS NULL))         AS EMPTY_KEYS,
                   COUNT_IF(T_DAT > DATEADD('day', -7, '{d}') AND T_DAT <= '{d}'
                            AND (PRICE IS NULL OR PRICE <= 0 OR PRICE > {PRICE_LIMIT})) AS BAD_PRICE
            FROM {mt.TX}
            WHERE T_DAT > DATEADD('day', -35, '{d}') AND T_DAT <= '{d}'
        """).iloc[0]
        last3, last7, prev28, empty, bad_price = (float(r[c]) for c in
                                                  ["LAST_3D", "LAST_7D", "PREV_28D", "EMPTY_KEYS", "BAD_PRICE"])
        add("fresh_purchases_last_3_days", last3, "> 0", last3 > 0)
        add("empty_ids_last_7_days", empty, "= 0", empty == 0)
        price_share = bad_price / max(last7, 1)
        add("bad_price_share", round(price_share, 4), f"<= {BAD_PRICE_MAX}", price_share <= BAD_PRICE_MAX,
            detail=f"{int(bad_price)} of {int(last7)} purchases: empty, <= 0 or > {PRICE_LIMIT}")
        weekly = prev28 / 4 if prev28 else 0
        change = (last7 / weekly - 1) if weekly else 0.0
        add("volume_change_vs_4_weeks", round(change, 4), f"within +/-{int(VOLUME_TOLERANCE * 100)}%",
            abs(change) <= VOLUME_TOLERANCE, warn_only=True,
            detail=f"last 7 days {int(last7)}, weekly avg {int(weekly)}")

        future = mt.q(f"SELECT COUNT(*) AS N FROM {mt.TX} WHERE T_DAT > CURRENT_DATE()")["N"].iloc[0]
        add("purchases_dated_after_today", float(future), "= 0", int(future) == 0)

        u = mt.q(f"""
            SELECT COUNT(*) AS N, COUNT_IF(a.ARTICLE_ID IS NULL) AS UNKNOWN_ART,
                   COUNT_IF(c.CUSTOMER_ID IS NULL) AS UNKNOWN_CUS
            FROM {mt.TX} t
            LEFT JOIN {mt.ART} a ON a.ARTICLE_ID = t.ARTICLE_ID
            LEFT JOIN {mt.CUS} c ON c.CUSTOMER_ID = t.CUSTOMER_ID
            WHERE t.T_DAT > DATEADD('day', -7, '{d}') AND t.T_DAT <= '{d}'
        """).iloc[0]
        n = max(float(u["N"]), 1)
        for name, col in [("unknown_article_share", "UNKNOWN_ART"), ("unknown_customer_share", "UNKNOWN_CUS")]:
            share = float(u[col]) / n
            add(name, round(share, 4), f"<= {UNKNOWN_MAX}", share <= UNKNOWN_MAX)

        a = mt.q(f"""
            SELECT COUNT(*) AS N,
                   COUNT_IF(NULLIF(TRIM(a.PRODUCT_TYPE_NAME), '') IS NULL
                         OR NULLIF(TRIM(a.PRODUCT_GROUP_NAME), '') IS NULL
                         OR NULLIF(TRIM(a.PERCEIVED_COLOUR_MASTER_NAME), '') IS NULL
                         OR NULLIF(TRIM(a.GRAPHICAL_APPEARANCE_NAME), '') IS NULL
                         OR NULLIF(TRIM(a.INDEX_GROUP_NAME), '') IS NULL) AS MISSING
            FROM {mt.ART} a
            WHERE a.ARTICLE_ID IN (SELECT DISTINCT ARTICLE_ID FROM {mt.TX}
                                   WHERE T_DAT > DATEADD('day', -28, '{d}') AND T_DAT <= '{d}')
        """).iloc[0]
        share = float(a["MISSING"]) / max(float(a["N"]), 1)
        add("articles_missing_attributes_share", round(share, 4), f"<= {MISSING_ATTR_MAX}",
            share <= MISSING_ATTR_MAX, detail=f"{int(a['MISSING'])} of {int(a['N'])} articles sold in 28 days")

        rep = mt.q(f"""
            SELECT IFF(T_DAT > DATEADD('day', -7, '{d}'), 'LAST', 'PREV') AS P,
                   SUM(N) AS ROWS_, SUM(N - 1) AS REPEATS
            FROM (SELECT T_DAT, CUSTOMER_ID, ARTICLE_ID, PRICE, COUNT(*) AS N FROM {mt.TX}
                  WHERE T_DAT > DATEADD('day', -35, '{d}') AND T_DAT <= '{d}'
                  GROUP BY 1, 2, 3, 4)
            GROUP BY 1
        """).set_index("P")

        def rate(p):
            return float(rep.loc[p, "REPEATS"]) / max(float(rep.loc[p, "ROWS_"]), 1) if p in rep.index else 0.0

        shift = rate("LAST") - rate("PREV")
        add("repeated_rows_shift", round(shift, 4), f"within +/-{REPEAT_ROWS_SHIFT}",
            abs(shift) <= REPEAT_ROWS_SHIFT, warn_only=True,
            detail=f"last 7 days {rate('LAST'):.3f}, 4 weeks before {rate('PREV'):.3f}")

    mt.session.sql(f"DELETE FROM {mt.DB}.NBO_DATA_CHECKS WHERE RUN_DATE = '{d}'").collect()
    _write(pd.DataFrame(rows), "NBO_DATA_CHECKS")
    statuses = [x["STATUS"] for x in rows]
    status = "FAILED" if "FAIL" in statuses else ("WARN" if "WARN" in statuses else "SUCCESS")
    return status, {x["CHECK_NAME"]: f"{x['STATUS']} ({x['VALUE']})" for x in rows}


# ---------------- send metrics ----------------
def save_metrics(d, metrics):
    """Numeric quality numbers of a send -> NBO_RUN_METRICS (replacing the date's rows)."""
    d = mt.ymd(d)
    rows = [dict(RUN_DATE=d, METRIC=k, VALUE=float(v)) for k, v in metrics.items() if isinstance(v, (int, float))]
    mt.session.sql(f"DELETE FROM {mt.DB}.NBO_RUN_METRICS WHERE RUN_DATE = '{d}'").collect()
    _write(pd.DataFrame(rows), "NBO_RUN_METRICS")
    return len(rows)


def save_send(d, wide, items):
    """Store a send's picks (replacing the date: re-run safe)."""
    d = mt.ymd(d)
    for t in ("NBO_RECOMMENDATIONS", "NBO_RECOMMENDATION_ITEMS"):
        mt.session.sql(f"DELETE FROM {mt.DB}.{t} WHERE RUN_DATE = '{d}'").collect()
    w = pd.DataFrame(wide)
    it = pd.DataFrame(items)
    if len(it):
        it["ARTICLE_ID"] = it["ARTICLE_ID"].astype(str)
        it["PRODUCT_CODE"] = pd.to_numeric(it["PRODUCT_CODE"], errors="coerce").astype("Int64")
        it["SCORE"] = pd.to_numeric(it["SCORE"], errors="coerce")
    _write(w, "NBO_RECOMMENDATIONS")
    _write(it, "NBO_RECOMMENDATION_ITEMS")
