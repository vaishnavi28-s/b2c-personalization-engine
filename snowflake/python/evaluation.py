from __future__ import annotations

# ======================================================================
# evaluation.py - did the recommendations lead to purchases?
#
# The system ends when the email is sent. What customers did afterwards is read from the purchase data
# (the H&M transactions run until 2020-09-22, so every send up to late June 2020 has its full 90 days):
#   NBO_EMAIL_EVENTS   'sent' (one row per card of every real email) and 'buy' (the customer bought the
#                      card's item - or the same product in another colour - within 90 days)
#   NBO_EVAL_CUSTOMERS one row per customer per evaluated send: emailed or holdout, bought anything, spend
#   NBO_EVAL_RESULTS   buy rates per slot, per position, per email; emailed vs holdout per send AND pooled over
#                      all evaluated sends (one send's 5% holdout is too small to show a realistic difference)
#
# Opens and clicks are NOT part of this build: in a real company they come from the shop website's own
# tracking (pixel + redirect link -> event files -> the same NBO_EMAIL_EVENTS table, EVENT_TYPE open / click),
# and engagement becomes clicks x 1 + buys x 3. The slot / position / holdout scoring below stays the same.
#
#   Part 1  events        write "sent", match buys from the purchases
#   Part 2  scores        buy rates per slot (which logic) and position (where it was shown)
#   Part 3  holdout       emailed vs never-emailed customers (lift), per send and pooled
#   Part 4  run           one evaluation of one send date
# Imported by procedures/nbo_evaluate.sql and nbo_events.sql. Call model_training.init(session) first.
# ======================================================================
import numpy as np
import pandas as pd

import model_training as mt

HORIZON_DAYS   = 90              # a buy counts if it happens within 90 days after the send
N_BOOT, SEED   = 1000, 42
Z_ALPHA, Z_POWER = 1.96, 0.8416  # minimum detectable lift: 5% two-sided test, 80% power
HOLDOUT_METRICS = (("any_buy_rate", "ANY_BUY"), ("spend_per_customer", "SPEND"), ("picked_item_buy_rate", "PICK_BUY"))


def _db():
    return mt.DB.split(".")


def _write(df, table):
    db, schema = _db()
    if len(df):
        mt.session.write_pandas(df, table, database=db, schema=schema, auto_create_table=False)


# ======================================================================
# PART 1 - EVENTS
# ======================================================================
def record_sent(d):
    """One 'sent' event per card of every real email of send date d (holdout customers get none).
    Re-run safe: the date's 'sent' rows are replaced."""
    d = mt.ymd(d)
    mt.session.sql(f"DELETE FROM {mt.DB}.NBO_EMAIL_EVENTS WHERE RUN_DATE = '{d}' AND EVENT_TYPE = 'sent'").collect()
    mt.session.sql(f"""
        INSERT INTO {mt.DB}.NBO_EMAIL_EVENTS
            (EVENT_ID, EMAIL_ID, RUN_DATE, CUSTOMER_ID, GRP, SLOT, POSITION, ARTICLE_ID, PRODUCT_CODE,
             EVENT_TYPE, EVENT_TIME, SOURCE, DETAIL)
        SELECT EMAIL_ID || '|sent|' || POS, EMAIL_ID, RUN_DATE, CUSTOMER_ID, GRP, POS, POSITION, ARTICLE_ID,
               PRODUCT_CODE, 'sent', RUN_DATE, 'send', ''
        FROM {mt.DB}.NBO_RECOMMENDATION_ITEMS
        WHERE RUN_DATE = '{d}' AND GRP = 'EMAIL'
    """).collect()
    n = mt.q(f"SELECT COUNT(*) AS N FROM {mt.DB}.NBO_EMAIL_EVENTS WHERE RUN_DATE = '{d}' AND EVENT_TYPE = 'sent'")
    return int(n["N"].iloc[0])


def match_buys(d, horizon=HORIZON_DAYS):
    """'buy' events: the customer bought a card's item (or the same product in another colour) within
    `horizon` days after the send. Done for EMAIL and HOLDOUT cards (holdout = what we would have shown).
    One buy per card (the first purchase). Re-run safe: the date's buy rows are replaced."""
    d = mt.ymd(d)
    mt.session.sql(f"DELETE FROM {mt.DB}.NBO_EMAIL_EVENTS WHERE RUN_DATE = '{d}' AND EVENT_TYPE = 'buy'").collect()
    mt.session.sql(f"""
        INSERT INTO {mt.DB}.NBO_EMAIL_EVENTS
            (EVENT_ID, EMAIL_ID, RUN_DATE, CUSTOMER_ID, GRP, SLOT, POSITION, ARTICLE_ID, PRODUCT_CODE,
             EVENT_TYPE, EVENT_TIME, SOURCE, DETAIL)
        WITH cards AS (
            SELECT EMAIL_ID, RUN_DATE, CUSTOMER_ID, GRP, POS, POSITION, ARTICLE_ID, PRODUCT_CODE
            FROM {mt.DB}.NBO_RECOMMENDATION_ITEMS WHERE RUN_DATE = '{d}'
        ),
        bought AS (
            SELECT c.EMAIL_ID, c.RUN_DATE, c.CUSTOMER_ID, c.GRP, c.POS, c.POSITION, c.ARTICLE_ID, c.PRODUCT_CODE,
                   t.T_DAT, CASE WHEN t.ARTICLE_ID = c.ARTICLE_ID THEN 'item' ELSE 'product' END AS LEVEL,
                   ROW_NUMBER() OVER (PARTITION BY c.EMAIL_ID, c.POS
                                      ORDER BY CASE WHEN t.ARTICLE_ID = c.ARTICLE_ID THEN 0 ELSE 1 END, t.T_DAT) AS RN
            FROM cards c
            JOIN {mt.TX} t ON t.CUSTOMER_ID = c.CUSTOMER_ID
            JOIN {mt.ART} a ON a.ARTICLE_ID = t.ARTICLE_ID AND a.PRODUCT_CODE = c.PRODUCT_CODE
            WHERE t.T_DAT > '{d}' AND t.T_DAT <= DATEADD('day', {int(horizon)}, '{d}')
        )
        SELECT EMAIL_ID || '|buy|' || POS, EMAIL_ID, RUN_DATE, CUSTOMER_ID, GRP, POS, POSITION, ARTICLE_ID,
               PRODUCT_CODE, 'buy', T_DAT, 'orders', LEVEL
        FROM bought WHERE RN = 1
    """).collect()
    n = mt.q(f"SELECT COUNT(*) AS N FROM {mt.DB}.NBO_EMAIL_EVENTS WHERE RUN_DATE = '{d}' AND EVENT_TYPE = 'buy'")
    return int(n["N"].iloc[0])


# ======================================================================
# PART 2 - SCORES: per slot, per position, per email
# ======================================================================
def ci_mean(values, n_boot=N_BOOT, seed=SEED):
    """Mean with a 95% bootstrap range (resampling the cards / emails, in chunks to save memory)."""
    v = np.asarray(values, dtype=float)
    if len(v) == 0:
        return np.nan, np.nan, np.nan
    rng, means = np.random.default_rng(seed), []
    step = max(1, 2_000_000 // len(v))
    for s in range(0, n_boot, step):
        k = min(step, n_boot - s)
        means.append(v[rng.integers(0, len(v), (k, len(v)))].mean(axis=1))
    m = np.concatenate(means)
    return float(v.mean()), float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def card_table(d):
    """One row per card of the send (EMAIL and HOLDOUT): was its item / product bought within 90 days?"""
    d = mt.ymd(d)
    cards = mt.q(f"""SELECT EMAIL_ID, CUSTOMER_ID, GRP, POS AS SLOT, POSITION, ARTICLE_ID
                     FROM {mt.DB}.NBO_RECOMMENDATION_ITEMS WHERE RUN_DATE = '{d}'""")
    ev = mt.q(f"""SELECT EMAIL_ID, SLOT, EVENT_TIME AS BUY_TIME, DETAIL AS BUY_LEVEL
                  FROM {mt.DB}.NBO_EMAIL_EVENTS WHERE RUN_DATE = '{d}' AND EVENT_TYPE = 'buy'""")
    c = cards.set_index(["EMAIL_ID", "SLOT"]).join(ev.set_index(["EMAIL_ID", "SLOT"])).reset_index()
    c["BUY"] = c["BUY_TIME"].notna().astype(float)                  # same product (any colour)
    c["BUY_ITEM"] = (c["BUY_LEVEL"] == "item").astype(float)        # exactly the article shown
    return c


CARD_METRICS = {"buy_rate": "BUY", "buy_item_rate": "BUY_ITEM"}


def scores(c):
    """Rows for NBO_EVAL_RESULTS (emailed customers only):
       by slot (which logic) and by position (where it was shown): buy rate per card;
       by email: share of emails with at least one bought pick, picks bought per email."""
    out = []
    em = c[c["GRP"] == "EMAIL"]
    if em.empty:
        return out
    for by, col in (("slot", "SLOT"), ("position", "POSITION")):
        for key, g in em.groupby(col):
            for metric, src in CARD_METRICS.items():
                v, lo, hi = ci_mean(g[src])
                out.append(dict(GROUP_BY=by, GROUP_KEY=str(int(key)), METRIC=metric, VALUE=v, CI_LOW=lo, CI_HIGH=hi,
                                N=len(g)))
    per_email = em.groupby("EMAIL_ID").agg(BOUGHT=("BUY", "max"), BUYS=("BUY", "sum"))
    for metric, src in (("email_buy_rate", "BOUGHT"), ("buys_per_email", "BUYS")):
        v, lo, hi = ci_mean(per_email[src])
        out.append(dict(GROUP_BY="email", GROUP_KEY="all", METRIC=metric, VALUE=v, CI_LOW=lo, CI_HIGH=hi,
                        N=len(per_email)))
    return out


# ======================================================================
# PART 3 - HOLDOUT: did the email change anything?
# ======================================================================
def ci_diff(a, b, n_boot=N_BOOT, seed=SEED):
    """Difference of means a - b with a 95% bootstrap range."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) == 0 or len(b) == 0:
        return np.nan, np.nan, np.nan
    rng = np.random.default_rng(seed)
    diffs = [a[rng.integers(0, len(a), len(a))].mean() - b[rng.integers(0, len(b), len(b))].mean()
             for _ in range(n_boot)]
    return float(a.mean() - b.mean()), float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def customer_outcomes(d, c, horizon=HORIZON_DAYS):
    """One row per customer of send d (emailed and holdout), over the 90 days after the send:
    ANY_BUY (bought anything), SPEND, PICK_BUY (bought one of her picks - for the holdout: what we would have shown)."""
    d = mt.ymd(d)
    cust = c.drop_duplicates("CUSTOMER_ID")[["CUSTOMER_ID", "GRP"]]
    db, schema = _db()
    mt.session.write_pandas(cust[["CUSTOMER_ID"]], "NBO_TMP_EVAL_CUST", database=db, schema=schema,
                            auto_create_table=True, overwrite=True, table_type="temporary")
    spend = mt.q(f"""
        SELECT t.CUSTOMER_ID, COUNT(*) AS N_BUYS, SUM(t.PRICE) AS SPEND
        FROM {mt.TX} t JOIN {mt.DB}.NBO_TMP_EVAL_CUST x ON x.CUSTOMER_ID = t.CUSTOMER_ID
        WHERE t.T_DAT > '{d}' AND t.T_DAT <= DATEADD('day', {int(horizon)}, '{d}')
        GROUP BY t.CUSTOMER_ID
    """)
    cust = cust.merge(spend, on="CUSTOMER_ID", how="left").fillna({"N_BUYS": 0, "SPEND": 0})
    cust["ANY_BUY"] = (cust["N_BUYS"].astype(float) > 0).astype(float)
    cust["SPEND"] = cust["SPEND"].astype(float)
    picked = c.groupby("CUSTOMER_ID")["BUY"].max().rename("PICK_BUY")
    cust = cust.merge(picked, on="CUSTOMER_ID", how="left").fillna({"PICK_BUY": 0})
    cust["RUN_DATE"] = d
    return cust[["RUN_DATE", "CUSTOMER_ID", "GRP", "ANY_BUY", "SPEND", "PICK_BUY"]]


def min_detectable(a, b):
    """Smallest true difference of means a - b this sample would detect (5% two-sided, 80% power)."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 2 or len(b) < 2:
        return np.nan
    return float((Z_ALPHA + Z_POWER) * np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)))


def compare(cust, group_by):
    """Emailed minus holdout for each metric, with its 95% range, the holdout level and the minimum detectable lift."""
    out = []
    e, h = cust[cust["GRP"] == "EMAIL"], cust[cust["GRP"] == "HOLDOUT"]
    for metric, col in HOLDOUT_METRICS:
        v, lo, hi = ci_diff(e[col], h[col])
        out.append(dict(GROUP_BY=group_by, GROUP_KEY="email_minus_holdout", METRIC=metric, VALUE=v,
                        CI_LOW=lo, CI_HIGH=hi, N=len(e) + len(h)))
        out.append(dict(GROUP_BY=group_by, GROUP_KEY="holdout_level", METRIC=metric,
                        VALUE=float(h[col].mean()) if len(h) else np.nan, CI_LOW=np.nan, CI_HIGH=np.nan, N=len(h)))
        out.append(dict(GROUP_BY=group_by, GROUP_KEY="min_detectable_lift", METRIC=metric,
                        VALUE=min_detectable(e[col], h[col]), CI_LOW=np.nan, CI_HIGH=np.nan, N=len(e) + len(h)))
    return out


def holdout(cust):
    """Emailed vs holdout customers of ONE send, over the same 90 days. Value = EMAIL minus HOLDOUT.
    In a replay nobody really received the email, so this is an A/A check: the range should include 0."""
    return compare(cust, "holdout")


def pooled(all_cust):
    """Emailed vs holdout over ALL evaluated sends up to this one. The unit is the customer (the holdout is stable:
    a customer is always in the same group), so each customer's outcomes are averaged over her sends first and the
    bootstrap resamples customers - repeated sends of the same person are not counted as independent evidence.
    One send has ~5% holdout; pooled, the range gets narrow enough to say something (min_detectable_lift shows how
    small a real lift this design could find)."""
    if all_cust.empty:
        return []
    per = all_cust.groupby(["CUSTOMER_ID", "GRP"], as_index=False)[[c for _, c in HOLDOUT_METRICS]].mean()
    return compare(per, "holdout_pooled")


# ======================================================================
# PART 4 - RUN ONE EVALUATION
# ======================================================================
def window_closed(d, horizon=HORIZON_DAYS):
    """True when the purchase data covers the full 90 days after the send."""
    last = mt.q(f"SELECT MAX(T_DAT) AS LAST_DAY FROM {mt.TX}")["LAST_DAY"].iloc[0]
    return pd.Timestamp(last) >= pd.Timestamp(d) + pd.Timedelta(days=horizon)


def evaluate(d, horizon=HORIZON_DAYS):
    """Evaluate one send date. Returns (status, result rows, info).
    PENDING  the 90 days are not over yet (nothing written)
    SUCCESS  buys matched, scores + holdout (this send and pooled over all sends up to it) written to
             NBO_EVAL_RESULTS, customer outcomes to NBO_EVAL_CUSTOMERS (replacing this date's rows)"""
    d = mt.ymd(d)
    info = dict(run_date=d, horizon_days=horizon)
    if not window_closed(d, horizon):
        return "PENDING", [], dict(info, note=f"purchase data does not cover {horizon} days after {d} yet")
    n_cards = mt.q(f"SELECT COUNT(*) AS N FROM {mt.DB}.NBO_RECOMMENDATION_ITEMS WHERE RUN_DATE = '{d}'")["N"].iloc[0]
    if int(n_cards) == 0:
        raise ValueError(f"No recommendations stored for {d}")
    has_sent = mt.q(f"""SELECT COUNT(*) AS N FROM {mt.DB}.NBO_EMAIL_EVENTS
                        WHERE RUN_DATE = '{d}' AND EVENT_TYPE = 'sent'""")["N"].iloc[0]
    info["sent_cards"] = int(has_sent) if int(has_sent) else record_sent(d)
    info["buys"] = match_buys(d, horizon)

    c = card_table(d)
    cust = customer_outcomes(d, c, horizon)
    mt.session.sql(f"DELETE FROM {mt.DB}.NBO_EVAL_CUSTOMERS WHERE RUN_DATE = '{d}'").collect()
    _write(cust, "NBO_EVAL_CUSTOMERS")
    all_cust = mt.q(f"""SELECT RUN_DATE, CUSTOMER_ID, GRP, ANY_BUY, SPEND, PICK_BUY
                        FROM {mt.DB}.NBO_EVAL_CUSTOMERS WHERE RUN_DATE <= '{d}'""")
    rows = scores(c) + holdout(cust) + pooled(all_cust)
    info.update(emails=int(c.loc[c["GRP"] == "EMAIL", "EMAIL_ID"].nunique()),
                holdout_customers=int(c.loc[c["GRP"] == "HOLDOUT", "CUSTOMER_ID"].nunique()),
                picks_bought=int(c.loc[c["GRP"] == "EMAIL", "BUY"].sum()),
                pooled_sends=int(all_cust["RUN_DATE"].nunique()),
                pooled_holdout_customers=int(all_cust.loc[all_cust["GRP"] == "HOLDOUT", "CUSTOMER_ID"].nunique()))
    for r in rows:
        r["RUN_DATE"] = d
    mt.session.sql(f"DELETE FROM {mt.DB}.NBO_EVAL_RESULTS WHERE RUN_DATE = '{d}'").collect()
    _write(pd.DataFrame(rows)[["RUN_DATE", "GROUP_BY", "GROUP_KEY", "METRIC", "VALUE", "CI_LOW", "CI_HIGH", "N"]],
           "NBO_EVAL_RESULTS")
    return "SUCCESS", rows, info
