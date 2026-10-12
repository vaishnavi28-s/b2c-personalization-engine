from __future__ import annotations

# ======================================================================
# model_training.py - the "cooking" part of the recommender
# Runs once per send date and prepares everything the 6 slots need:
#   Part 2  shelf          what is in the shop now (season + momentum, fair share per department)
#   Part 3  taste model    LightFM with features             -> slots 1, 2, 5, 6
#   Part 4  basket model   LightFM, top 500, no features     -> slot 3
#   Part 5  trending lists per age group x section           -> slot 4
#   Part 6  occasion       festival / month theme / season   -> slots 2, 6
#   Part 7  model check    train to send-15d, test next 15d  -> the model gate (+ best-seller baseline)
#   Part 8  train + save   (only on RETRAIN dates: the 1st of each month)
# Imported by the Snowflake procedures. Call init(session) first.
# The target schema (HM_ML or HM_ML_DEV) is the session's current schema, so the same code runs in DEV and PROD.
# ======================================================================
import io
import os
import pickle
import time

import numpy as np
import pandas as pd
from lightfm import LightFM
from lightfm.data import Dataset
from lightfm.evaluation import precision_at_k

# ======================================================================
# PART 1 - SETUP AND SETTINGS
# ======================================================================
session = None
DB = None                       # e.g. NBO_REBUILD.HM_ML  (where our NBO_* tables and stages live)
TX = ART = CUS = None           # purchases, products, customers (read-only source data)
SOURCE_SCHEMA = "HM_ODS"


def q(sql):
    """Run SQL in Snowflake, return a pandas table."""
    return session.sql(sql).to_pandas()


def _clean(name):
    return str(name or "").strip('"')


def init(snowpark_session, target=None):
    """Connect to Snowflake. target = 'DB.SCHEMA' for our tables; default = the session's current schema."""
    global session, DB, TX, ART, CUS
    session = snowpark_session
    if target is None:
        target = f"{_clean(session.get_current_database())}.{_clean(session.get_current_schema())}"
    DB = target
    src = f"{target.split('.')[0]}.{SOURCE_SCHEMA}"
    TX, ART, CUS = (f"{src}.{n}" for n in
                    ["HM_NBO_DWH_F_TRANSACTION", "HM_NBO_DWH_D_ARTICLE", "HM_NBO_DWH_D_CUSTOMER"])
    applied = apply_settings()
    return f"ready | tables in {DB} | source {src} | overrides {applied or 'none'}"


# ---------------- per-schema overrides (table NBO_SETTINGS) ----------------
# DEV and PROD leave NBO_SETTINGS empty = the real settings below. Only the short-lived CI test schema writes
# smaller numbers there (fewer epochs) so a full pipeline test finishes in minutes. Only these names may change:
OVERRIDABLE = {"TASTE_EPOCHS": ("TASTE", "epochs"), "BASKET_EPOCHS": ("BASKET", "epochs"),
               "EVAL_USERS": (None, "EVAL_USERS"), "POOL_SIZE": (None, "POOL_SIZE")}
_DEFAULTS = {}


def apply_settings():
    """Reset to the defaults, then apply the rows of NBO_SETTINGS (if the table has any). Returns what changed."""
    g = globals()
    if not _DEFAULTS:
        _DEFAULTS.update(TASTE=dict(g["TASTE"]), BASKET=dict(g["BASKET"]),
                         EVAL_USERS=g["EVAL_USERS"], POOL_SIZE=g["POOL_SIZE"])
    g["TASTE"], g["BASKET"] = dict(_DEFAULTS["TASTE"]), dict(_DEFAULTS["BASKET"])
    g["EVAL_USERS"], g["POOL_SIZE"] = _DEFAULTS["EVAL_USERS"], _DEFAULTS["POOL_SIZE"]
    try:
        rows = q(f"SELECT NAME, VALUE FROM {DB}.NBO_SETTINGS")
    except Exception:
        return {}
    applied = {}
    for name, value in zip(rows["NAME"], rows["VALUE"]):
        key = str(name).upper()
        if key not in OVERRIDABLE:
            raise ValueError(f"NBO_SETTINGS: {name} cannot be overridden (allowed: {sorted(OVERRIDABLE)})")
        group, field = OVERRIDABLE[key]
        if group:
            g[group][field] = int(value)
        else:
            g[field] = int(value)
        applied[key] = int(value)
    return applied


def code_version():
    """The git commit of the code deployed in this schema (written by deploy.py into NBO_DEPLOYMENTS)."""
    try:
        r = q(f"SELECT CODE_VERSION FROM {DB}.NBO_DEPLOYMENTS ORDER BY DEPLOYED_AT DESC LIMIT 1")
        return str(r["CODE_VERSION"].iloc[0]) if len(r) else "unknown"
    except Exception:
        return "unknown"


# ---------------- settings, grouped by part ----------------
GARMENTS = "('Garment Upper body', 'Garment Lower body', 'Garment Full body', 'Swimwear')"
SEED = 44

# Part 2 - shelf
POOL_DAYS        = 28            # "in the shop now" = sold in the last 28 days
POOL_SIZE        = 2000          # top 2000 products
SEASON_MIN_SALES = 5000          # a type needs this many sales last year before its season boost counts
MOMENTUM_RANGE   = (0.5, 2.0)    # rising/falling sales change the score by at most 2x

# Part 3 - taste model
TRAIN_DAYS  = 180                # history window
MIN_ITEMS   = 3                  # a customer needs >= 3 different items
HALF_LIFE   = 30                 # a buy 30 days ago counts half
MIN_WEIGHT  = 0.1                # an old buy never counts less than 0.1
MAX_REPEATS = 3                  # the same item bought 10 times counts at most 3 times
TASTE = dict(no_components=35, learning_rate=0.04, epochs=200)      # baseline settings

# Part 4 - basket model
BASKET_TOP = 500                                                    # top 500 best sellers
BASKET = dict(no_components=10, learning_rate=0.05, epochs=100)     # sales-only settings

# Part 5 - trending
TREND_HALF_LIFE  = 60            # a sale 60 days ago counts half
TREND_TOP        = 100           # top 100 per list
TREND_MIN_BUYERS = 20            # an item needs >= 20 buyers in the group (one big order can't make it trend)

# Part 6 - occasion (festival / month theme / season)
SEASON_MIN = 1.3                 # "in season" = sold >= 1.3x its normal month, same month last year
EVENT_MIN, EVENT_CAP, EVENT_TOP = 1.5, 5, 25   # festival: type+pattern sold >= 1.5x before last year's festival
EVENT_TYPES = {                  # festivals with a known look: these types always count (plus the data-driven ones)
    "christmas": ("Dress", "Sweater", "Cardigan", "Blouse", "Top", "Skirt"),
}
EVENT_BOOST_PATTERN = {"christmas": "Glittering/Metallic"}       # festive pattern shown first
THEME_COLS = {"INDEX_GROUP_NAME": "DEPT", "PRODUCT_GROUP_NAME": "PGROUP", "PRODUCT_TYPE_NAME": "PTYPE"}
DEFAULT_SEASON_RULES = pd.DataFrame([          # fallback copy of table NBO_SEASON_RULES
    ("Swimwear",           None,     "9,10,11,12,1,2,3,4", "no swimwear outside summer"),
    ("Garment Lower body", "Shorts", "10,11,12,1,2,3",     "no shorts in cold months"),
    ("Garment Upper body", "Coat",   "5,6,7,8",            "no coats in summer"),
], columns=["PRODUCT_GROUP", "PRODUCT_TYPE", "BLOCKED_MONTHS", "REASON"])

# Part 7 - model check
VALID_DAYS = 15                  # train up to send-15 days, test on the 15 days after
EVAL_USERS = 20000               # measure on a sample of customers (speed)

# ---------------- columns we read ----------------
AGE_GROUP_SQL = """CASE WHEN c.AGE IS NULL THEN 'unknown'
                        WHEN c.AGE < 20 THEN 'teen'
                        WHEN c.AGE < 30 THEN '20s'
                        WHEN c.AGE < 40 THEN '30s'
                        WHEN c.AGE < 50 THEN '40s'
                        ELSE '50plus' END"""

ITEM_COLS = """a.ARTICLE_ID, a.PRODUCT_CODE, a.PROD_NAME,
               a.INDEX_GROUP_NAME AS DEPT, a.SECTION_NAME AS SECTION,
               a.PRODUCT_TYPE_NAME AS PTYPE, a.PRODUCT_GROUP_NAME AS PGROUP,
               a.PERCEIVED_COLOUR_MASTER_NAME AS COLOUR, a.GRAPHICAL_APPEARANCE_NAME AS PATTERN"""

USER_COLS = f"""c.CUSTOMER_ID,
                {AGE_GROUP_SQL}                                AS AGE_GROUP,
                COALESCE(c.CLUB_MEMBER_STATUS, 'unknown')      AS CLUB,
                COALESCE(c.FASHION_NEWS_FREQUENCY, 'unknown')  AS NEWS"""


# ---------------- small helpers ----------------
def ymd(x):
    """Any date -> 'YYYY-MM-DD'."""
    return pd.Timestamp(x).strftime("%Y-%m-%d")


def tok(prefix, value):
    """Feature label, e.g. ('colour', 'Dark Blue') -> 'colour:dark_blue'."""
    return f"{prefix}:{str(value).strip().lower().replace(' ', '_')}"


def item_tokens(r):
    """What the taste model knows about an item."""
    return [tok("dept", r.DEPT), tok("type", r.PTYPE), tok("group", r.PGROUP),
            tok("colour", r.COLOUR), tok("pattern", r.PATTERN)]


def user_tokens(r):
    """What the taste model knows about a customer."""
    return [tok("age", r.AGE_GROUP), tok("club", r.CLUB), tok("news", r.NEWS)]


def section_of(dept, section):
    """Who an item is for: Women / Men / Young / Kids (H&M has no gender field).
    Young = Divided, H&M's young-fashion department."""
    dept = pd.Series(dept).astype(str).reset_index(drop=True)
    sec = pd.Series(section).fillna("").astype(str).str.lower().reset_index(drop=True)
    out = pd.Series("Women", index=dept.index)                  # Ladieswear, ladies sport
    out[dept.eq("Divided")] = "Young"
    out[dept.eq("Menswear")] = "Men"
    out[dept.eq("Baby/Children")] = "Kids"
    sport = dept.eq("Sport")
    out[sport & sec.str.startswith("men")] = "Men"
    out[sport & sec.str.contains("kid|girl|boy|baby")] = "Kids"
    return out.values


def ids_sql(values):
    """['a', 'b'] -> "'a','b'" for an IN (...) list."""
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in values)


# ---------------- save / load models on the Snowflake stage ----------------
def stage():
    return f"@{DB}.NBO_OUTPUT_STAGE/models"


def save_model(M, name):
    session.file.put_stream(io.BytesIO(pickle.dumps(M)), f"{stage()}/{name}.pkl",
                            auto_compress=False, overwrite=True)


def load_model(name):
    with session.file.get_stream(f"{stage()}/{name}.pkl") as f:
        return pickle.load(f)


# ======================================================================
# PART 2 - SHELF: what is in the shop now
# Score = sales in the last 28 days x season boost x momentum; every department gets a fair share of the 2000 places.
# ======================================================================
def season_index(d, col):
    """For each value of `col` (e.g. product type): how much it sold in this month LAST year vs its average month.
    1.0 = normal, 2.0 = sells twice as much as usual in this month. Small types get 1.0."""
    t = pd.Timestamp(d)
    ly_start = f"{t.year - 1}-{t.month:02d}-01"
    s = q(f"""
        SELECT a.{col} AS NAME,
               COUNT_IF(t.T_DAT >= '{ly_start}' AND t.T_DAT < DATEADD('month', 1, '{ly_start}')) AS MONTH_SALES,
               COUNT(*) AS YEAR_SALES
        FROM {TX} t JOIN {ART} a ON a.ARTICLE_ID = t.ARTICLE_ID
        WHERE t.T_DAT > DATEADD('day', -365, '{d}') AND t.T_DAT <= '{d}'
          AND a.PRODUCT_GROUP_NAME IN {GARMENTS}
        GROUP BY a.{col}
        HAVING COUNT(*) >= 100
    """)
    s["INDEX"] = (s["MONTH_SALES"].astype(float) / (s["YEAR_SALES"].astype(float) / 12)).round(2)
    s.loc[s["YEAR_SALES"].astype(float) < SEASON_MIN_SALES, "INDEX"] = 1.0
    return s.set_index("NAME")["INDEX"]


def build_pool(d, size=POOL_SIZE, save=True):
    d = ymd(d)
    by_type, by_dept = season_index(d, "PRODUCT_TYPE_NAME"), season_index(d, "INDEX_GROUP_NAME")
    p = q(f"""
        WITH art AS (
            SELECT a.PRODUCT_CODE, a.ARTICLE_ID, a.PROD_NAME, a.PRODUCT_TYPE_NAME, a.INDEX_GROUP_NAME,
                   COUNT(*) AS SALES_28D,
                   COUNT_IF(t.T_DAT > DATEADD('day', -14, '{d}')) AS LAST_14D
            FROM {TX} t JOIN {ART} a ON a.ARTICLE_ID = t.ARTICLE_ID
            WHERE t.T_DAT > DATEADD('day', -{POOL_DAYS}, '{d}') AND t.T_DAT <= '{d}'
              AND a.PRODUCT_GROUP_NAME IN {GARMENTS}
            GROUP BY 1, 2, 3, 4, 5
        )
        SELECT PRODUCT_CODE,
               MAX_BY(ARTICLE_ID, SALES_28D)  AS BEST_ARTICLE_ID,
               MAX_BY(PROD_NAME, SALES_28D)   AS PROD_NAME,
               MODE(PRODUCT_TYPE_NAME)        AS PRODUCT_TYPE_NAME,
               MODE(INDEX_GROUP_NAME)         AS INDEX_GROUP_NAME,
               SUM(SALES_28D) AS SALES_28D, SUM(LAST_14D) AS LAST_14D
        FROM art GROUP BY PRODUCT_CODE
    """)
    for c in ["SALES_28D", "LAST_14D"]:
        p[c] = p[c].astype(float)
    p["PREV_14D"] = p["SALES_28D"] - p["LAST_14D"]
    p["MOMENTUM"] = ((p["LAST_14D"] + 1) / (p["PREV_14D"] + 1)).clip(*MOMENTUM_RANGE).round(2)
    p["SEASON_INDEX"] = p["PRODUCT_TYPE_NAME"].map(by_type).fillna(1.0).astype(float)
    p["POOL_SCORE"] = (p["SALES_28D"] * p["SEASON_INDEX"] * p["MOMENTUM"]).round(1)

    # fair share: each department gets places in proportion to its sales x its own season boost
    share = p.groupby("INDEX_GROUP_NAME")["SALES_28D"].sum()
    weighted = (share / share.sum()) * share.index.map(by_dept).fillna(1.0).values.astype(float)
    quota = (weighted / weighted.sum() * size).round().astype(int)
    p["DEPT_RANK"] = p.groupby("INDEX_GROUP_NAME")["POOL_SCORE"].rank(ascending=False, method="first")
    out = p[p["DEPT_RANK"] <= p["INDEX_GROUP_NAME"].map(quota)].copy().reset_index(drop=True)
    out["POOL_DATE"] = d
    if save:                                        # audit trail: what was on the shelf for this date
        session.sql(f"DELETE FROM {DB}.NBO_POOL_BY_DATE WHERE POOL_DATE = '{d}'").collect()
        session.write_pandas(out[["POOL_DATE", "PRODUCT_CODE", "BEST_ARTICLE_ID", "PROD_NAME", "PRODUCT_TYPE_NAME",
                                  "INDEX_GROUP_NAME", "SALES_28D", "MOMENTUM", "SEASON_INDEX", "POOL_SCORE"]],
                             "NBO_POOL_BY_DATE", database=DB.split(".")[0], schema=DB.split(".")[1],
                             auto_create_table=False)
    print(f"shelf {d} | {len(out)} products | places {quota.to_dict()}")
    return out


# ======================================================================
# PART 3 - TASTE MODEL (slots 1, 2, 5, 6)
# ======================================================================
def train_taste(d, pool):
    """LightFM with item + customer features; recent buys weigh more. Trained on ALL of the 180 days before d."""
    t0, d = time.time(), ymd(d)
    codes = ",".join(str(int(c)) for c in pool["PRODUCT_CODE"])
    window = f"""
        win AS (
            SELECT t.CUSTOMER_ID, t.ARTICLE_ID, t.T_DAT
            FROM {TX} t JOIN {ART} a ON a.ARTICLE_ID = t.ARTICLE_ID
            WHERE t.T_DAT > DATEADD('day', -{TRAIN_DAYS}, '{d}') AND t.T_DAT <= '{d}'
              AND a.PRODUCT_CODE IN ({codes})
        ),
        users AS (SELECT CUSTOMER_ID FROM win GROUP BY CUSTOMER_ID
                  HAVING COUNT(DISTINCT ARTICLE_ID) >= {MIN_ITEMS})"""

    # one row per customer + item; weight = sum of recency weights of her last 3 buys of it
    inter = q(f"""
        WITH {window},
        capped AS (
            SELECT w.* FROM win w JOIN users u ON u.CUSTOMER_ID = w.CUSTOMER_ID
            QUALIFY ROW_NUMBER() OVER (PARTITION BY w.CUSTOMER_ID, w.ARTICLE_ID ORDER BY w.T_DAT DESC) <= {MAX_REPEATS}
        )
        SELECT CUSTOMER_ID, ARTICLE_ID,
               SUM(GREATEST(POWER(0.5, DATEDIFF('day', T_DAT, '{d}') / {HALF_LIFE}), {MIN_WEIGHT})) AS W
        FROM capped GROUP BY CUSTOMER_ID, ARTICLE_ID
    """)
    inter["W"] = inter["W"].astype(float)
    users = q(f"WITH {window} SELECT {USER_COLS} FROM {CUS} c JOIN users u ON u.CUSTOMER_ID = c.CUSTOMER_ID")
    items = q(f"SELECT {ITEM_COLS} FROM {ART} a WHERE a.PRODUCT_CODE IN ({codes})")
    items = items[items["ARTICLE_ID"].isin(set(inter["ARTICLE_ID"]))].reset_index(drop=True)
    items["SEC"] = section_of(items["DEPT"], items["SECTION"])

    u_tok = {r.CUSTOMER_ID: user_tokens(r) for r in users.itertuples()}
    i_tok = {r.ARTICLE_ID: item_tokens(r) for r in items.itertuples()}
    inter = inter[inter["ARTICLE_ID"].isin(i_tok) & inter["CUSTOMER_ID"].isin(u_tok)].reset_index(drop=True)

    ds = Dataset()                         # identity features on: each item / customer is also its own feature
    ds.fit(users=list(u_tok), items=list(i_tok),
           user_features={t for ts in u_tok.values() for t in ts},
           item_features={t for ts in i_tok.values() for t in ts})
    uf = ds.build_user_features(u_tok.items())
    itf = ds.build_item_features(i_tok.items())
    train_m, train_w = ds.build_interactions(inter[["CUSTOMER_ID", "ARTICLE_ID", "W"]].itertuples(index=False, name=None))

    model = LightFM(loss="warp", no_components=TASTE["no_components"],
                    learning_rate=TASTE["learning_rate"], random_state=SEED)
    model.fit(train_m, sample_weight=train_w, user_features=uf, item_features=itf,
              epochs=TASTE["epochs"], num_threads=os.cpu_count())

    uid, _, iid, _ = ds.mapping()
    mins = round((time.time() - t0) / 60, 1)
    print(f"taste {d} | customers {len(uid):,} | items {len(iid):,} | {mins} min")
    return dict(kind="taste", date=d, model=model, dataset=ds, uid=uid, iid=iid, train_matrix=train_m,
                user_features=uf, item_features=itf, items=items, customers=users,
                minutes=mins, n_customers=len(uid), n_items=len(iid))


# ======================================================================
# PART 4 - BASKET MODEL (slot 3) - "sales only"
# ======================================================================
def train_basket(d, top_n=BASKET_TOP):
    """Pure collaborative filtering: top-500 sellers, no features, each item she bought counts once."""
    t0, d = time.time(), ymd(d)
    top = q(f"""
        SELECT t.ARTICLE_ID, COUNT(*) AS SALES
        FROM {TX} t JOIN {ART} a ON a.ARTICLE_ID = t.ARTICLE_ID
        WHERE t.T_DAT > DATEADD('day', -{TRAIN_DAYS}, '{d}') AND t.T_DAT <= '{d}'
          AND a.PRODUCT_GROUP_NAME IN {GARMENTS}
        GROUP BY t.ARTICLE_ID
        ORDER BY SALES DESC
        LIMIT {int(top_n)}
    """)
    ids = ids_sql(top["ARTICLE_ID"])
    inter = q(f"""
        WITH b AS (
            SELECT DISTINCT CUSTOMER_ID, ARTICLE_ID FROM {TX}
            WHERE T_DAT > DATEADD('day', -{TRAIN_DAYS}, '{d}') AND T_DAT <= '{d}' AND ARTICLE_ID IN ({ids})
        )
        SELECT b.CUSTOMER_ID, b.ARTICLE_ID FROM b
        JOIN (SELECT CUSTOMER_ID FROM b GROUP BY CUSTOMER_ID HAVING COUNT(*) >= {MIN_ITEMS}) u
          ON u.CUSTOMER_ID = b.CUSTOMER_ID
    """)
    items = q(f"SELECT {ITEM_COLS} FROM {ART} a WHERE a.ARTICLE_ID IN ({ids})")
    items = items.merge(top, on="ARTICLE_ID", how="left")
    items["SALES"] = items["SALES"].astype(float)
    items["SEC"] = section_of(items["DEPT"], items["SECTION"])

    ds = Dataset()
    ds.fit(users=inter["CUSTOMER_ID"].unique(), items=items["ARTICLE_ID"].unique())
    train_m, _ = ds.build_interactions(zip(inter["CUSTOMER_ID"], inter["ARTICLE_ID"]))

    model = LightFM(loss="warp", no_components=BASKET["no_components"],
                    learning_rate=BASKET["learning_rate"], random_state=SEED)
    model.fit(train_m, epochs=BASKET["epochs"], num_threads=os.cpu_count())   # no sample_weight: pure CF

    uid, _, iid, _ = ds.mapping()
    mins = round((time.time() - t0) / 60, 1)
    print(f"basket {d} | customers {len(uid):,} | items {len(iid):,} | {mins} min")
    return dict(kind="basket", date=d, model=model, dataset=ds, uid=uid, iid=iid, train_matrix=train_m,
                items=items.sort_values("SALES", ascending=False).reset_index(drop=True),
                minutes=mins, n_customers=len(uid), n_items=len(iid))


# ======================================================================
# PART 5 - TRENDING LISTS (slot 4): per age group x section, adult sections only
# ======================================================================
def build_trending(d):
    """Recency-weighted sales (60-day half-life). Returns {(age_group, section): top-100 table}:
      (age, 'Women'|'Men'|'Young')  the slot 4 list
      (age, 'all')                   her age group, all adult sections (fallback)
      ('all', section)               all ages (customers with unknown age)
      ('all', 'all')                 everyone (last fallback)"""
    d = ymd(d)
    s = q(f"""
        SELECT {AGE_GROUP_SQL} AS AGE_GROUP, t.ARTICLE_ID,
               SUM(POWER(0.5, DATEDIFF('day', t.T_DAT, '{d}') / {TREND_HALF_LIFE})) AS TREND_SCORE,
               COUNT(DISTINCT t.CUSTOMER_ID) AS BUYERS
        FROM {TX} t
        JOIN {ART} a ON a.ARTICLE_ID = t.ARTICLE_ID
        JOIN {CUS} c ON c.CUSTOMER_ID = t.CUSTOMER_ID
        WHERE t.T_DAT > DATEADD('day', -{TRAIN_DAYS}, '{d}') AND t.T_DAT <= '{d}'
          AND a.PRODUCT_GROUP_NAME IN {GARMENTS}
        GROUP BY 1, 2
    """)
    s["TREND_SCORE"] = s["TREND_SCORE"].astype(float)
    s["BUYERS"] = s["BUYERS"].astype(int)
    everyone = s.groupby("ARTICLE_ID", as_index=False)[["TREND_SCORE", "BUYERS"]].sum()
    everyone["AGE_GROUP"] = "all"
    s = pd.concat([s[s["AGE_GROUP"] != "unknown"], everyone], ignore_index=True)
    s = s[s["BUYERS"] >= TREND_MIN_BUYERS]
    if s.empty:
        return {}
    items = q(f"SELECT {ITEM_COLS} FROM {ART} a WHERE a.ARTICLE_ID IN ({ids_sql(s['ARTICLE_ID'].unique())})")
    items["SEC"] = section_of(items["DEPT"], items["SECTION"])
    s = s.merge(items, on="ARTICLE_ID")
    s = s[s["SEC"] != "Kids"]                       # slot 4 is "for her": adult sections only

    lists = {}
    for (age, sec), g in s.groupby(["AGE_GROUP", "SEC"]):
        lists[(age, sec)] = g.nlargest(TREND_TOP, "TREND_SCORE").reset_index(drop=True)
    for age, g in s.groupby("AGE_GROUP"):
        lists[(age, "all")] = g.nlargest(TREND_TOP, "TREND_SCORE").reset_index(drop=True)
    print(f"trending {d} | {len(lists)} lists")
    return lists


# ======================================================================
# PART 6 - OCCASION (slots 2, 6): festival -> month theme -> in season -> popular right now
# ======================================================================
def load_season_rules():
    try:
        r = q(f"SELECT PRODUCT_GROUP, PRODUCT_TYPE, BLOCKED_MONTHS, REASON FROM {DB}.NBO_SEASON_RULES")
        return r if len(r) else DEFAULT_SEASON_RULES
    except Exception:
        return DEFAULT_SEASON_RULES


def season_blocked(df, month, rules):
    """True for every row a season rule forbids in this month (e.g. coats in June)."""
    mask = pd.Series(False, index=df.index)
    for r in rules.itertuples():
        months = {int(m) for m in str(r.BLOCKED_MONTHS).replace(" ", "").split(",") if m}
        if int(month) not in months:
            continue
        m = pd.Series(True, index=df.index)
        if isinstance(r.PRODUCT_GROUP, str) and r.PRODUCT_GROUP:
            m &= df["PGROUP"].eq(r.PRODUCT_GROUP)
        if isinstance(r.PRODUCT_TYPE, str) and r.PRODUCT_TYPE:
            m &= df["PTYPE"].eq(r.PRODUCT_TYPE)
        mask |= m
    return mask


def calendar_row(d):
    """This send's row from NBO_SEND_CALENDAR. A date not in the calendar gets its month theme or 'Popular right now'."""
    d = ymd(d)
    cal = q(f"SELECT * FROM {DB}.NBO_SEND_CALENDAR WHERE SEND_DATE = '{d}'")
    if len(cal):
        r = cal.iloc[0].to_dict()
        r["SEND_DATE"], r["MODEL_DATE"] = ymd(r["SEND_DATE"]), ymd(r["MODEL_DATE"])
        return r
    month_start = ymd(pd.Timestamp(d).replace(day=1))
    row = dict(SEND_DATE=d, RETRAIN=(d == month_start), MODEL_DATE=month_start, OCCASION="Popular right now",
               OCC_TYPE="fallback", OCC_DATE=None, OCC_DATE_LY=None, OCC_DAYS=None, THEME_COL=None, THEME_VALUE=None)
    t = month_theme(pd.Timestamp(d).month)
    if t:
        row.update(OCCASION=t[0], OCC_TYPE="theme", THEME_COL=t[1], THEME_VALUE=t[2])
    return row


def month_theme(month):
    try:
        t = q(f"SELECT THEME, THEME_COL, THEME_VALUE FROM {DB}.NBO_MONTH_THEMES WHERE M = {int(month)}")
    except Exception:
        return None
    return tuple(t.iloc[0][["THEME", "THEME_COL", "THEME_VALUE"]]) if len(t) else None


def event_pairs(event_date_ly, days, send_date):
    """Data-driven festival look: (type, pattern) pairs that sold >= 1.5x their normal pace in the `days`
    before LAST year's festival. Returns the top 25 with their index."""
    d = q(f"""
        WITH s AS (
            SELECT a.PRODUCT_TYPE_NAME AS PTYPE, a.GRAPHICAL_APPEARANCE_NAME AS PATTERN,
                   COUNT_IF(t.T_DAT > DATEADD('day', -{int(days)}, '{event_date_ly}') AND t.T_DAT <= '{event_date_ly}') AS BEFORE_EVENT,
                   COUNT_IF(t.T_DAT > DATEADD('day', -365, '{send_date}')) AS YEAR_SALES
            FROM {TX} t JOIN {ART} a ON a.ARTICLE_ID = t.ARTICLE_ID
            WHERE t.T_DAT > LEAST(DATEADD('day', -{int(days)}, '{event_date_ly}'), DATEADD('day', -365, '{send_date}'))
              AND t.T_DAT <= '{send_date}'
              AND a.PRODUCT_GROUP_NAME IN {GARMENTS}
            GROUP BY 1, 2
        )
        SELECT PTYPE, PATTERN, BEFORE_EVENT, YEAR_SALES,
               ((BEFORE_EVENT + 10) / (YEAR_SALES * {int(days)} / 365 + 10))
               / (SUM(BEFORE_EVENT) OVER () / (SUM(YEAR_SALES) OVER () * {int(days)} / 365)) AS EVENT_INDEX
        FROM s WHERE YEAR_SALES >= 300
    """)
    if d.empty:
        return d
    d["EVENT_INDEX"] = d["EVENT_INDEX"].astype(float).clip(upper=EVENT_CAP)
    return d[d["EVENT_INDEX"] >= EVENT_MIN].nlargest(EVENT_TOP, "EVENT_INDEX").reset_index(drop=True)


def build_occasion(d):
    """What slots 2 and 6 show for this send. First rule that finds something wins:
       festival (data-driven pairs, + fixed look for Christmas) -> month theme -> in-season types -> popular right now."""
    d = ymd(d)
    cal = calendar_row(d)
    month = pd.Timestamp(d).month
    occ = dict(date=d, occasion=cal["OCCASION"], source="popular", pairs=set(), types=set(),
               boost_pattern=None, theme_col=None, theme_value=None, season_types=set(), calendar=cal)

    by_type = season_index(d, "PRODUCT_TYPE_NAME")
    occ["season_types"] = set(by_type[by_type >= SEASON_MIN].index)

    if cal["OCC_TYPE"] == "event" and cal.get("OCC_DATE_LY") is not None and pd.notna(cal.get("OCC_DATE_LY")):
        ev = event_pairs(ymd(cal["OCC_DATE_LY"]), int(cal["OCC_DAYS"]), d)
        key = next((k for k in EVENT_TYPES if k in str(cal["OCCASION"]).lower()), None)
        occ["pairs"] = set(zip(ev["PTYPE"], ev["PATTERN"])) if len(ev) else set()
        occ["types"] = set(EVENT_TYPES.get(key, ()))
        occ["boost_pattern"] = EVENT_BOOST_PATTERN.get(key)
        if occ["pairs"] or occ["types"]:
            occ["source"] = "festival"
            print(f"occasion {d} | festival {cal['OCCASION']} | {len(occ['pairs'])} data pairs | fixed types {sorted(occ['types'])}")
            return occ

    theme = (cal["OCCASION"], cal["THEME_COL"], cal["THEME_VALUE"]) if cal["OCC_TYPE"] == "theme" else month_theme(month)
    if theme and theme[1] in THEME_COLS:
        occ.update(source="theme", occasion=theme[0], theme_col=THEME_COLS[theme[1]], theme_value=theme[2])
        print(f"occasion {d} | theme {theme[0]} ({theme[1]} = {theme[2]})")
        return occ

    if occ["season_types"]:
        occ.update(source="season", occasion="In season now")
        print(f"occasion {d} | in season: {sorted(occ['season_types'])}")
        return occ

    occ.update(source="popular", occasion="Popular right now")
    print(f"occasion {d} | popular right now")
    return occ


def occasion_mask(df, occ):
    """True for items that belong to this send's occasion."""
    if occ["source"] == "festival":
        pair = pd.Series(list(zip(df["PTYPE"], df["PATTERN"])), index=df.index).isin(occ["pairs"])
        return pair | df["PTYPE"].isin(occ["types"])
    if occ["source"] == "theme":
        return df[occ["theme_col"]].eq(occ["theme_value"])
    if occ["source"] == "season":
        return df["PTYPE"].isin(occ["season_types"])
    return pd.Series(True, index=df.index)          # popular right now: everything on the shelf


# ======================================================================
# PART 7 - MODEL CHECK (the gate): train on the past, test on the future, compare with best sellers
# ======================================================================
BASELINE_DAYS = 7                # the naive baseline: the best sellers of the 7 days before the check date


def best_sellers(start, days=BASELINE_DAYS, top=1000):
    """Articles ranked by purchases in the `days` days up to `start` (most bought first)."""
    r = q(f"""SELECT ARTICLE_ID, COUNT(*) AS N FROM {TX}
              WHERE T_DAT > DATEADD('day', -{int(days)}, '{ymd(start)}') AND T_DAT <= '{ymd(start)}'
              GROUP BY ARTICLE_ID ORDER BY N DESC, ARTICLE_ID LIMIT {int(top)}""")
    return list(r["ARTICLE_ID"])


def baseline_precision(item_index, ranked, test_m, train_m, k=5):
    """precision@k of "show everyone this week's best sellers", scored exactly like the model: the same customers
    (those with test buys), the same item universe (item_index = the model's article -> column map), the same test
    buys, and items already in the customer's training history skipped. None when no customer can be scored."""
    order = [item_index[a] for a in ranked if a in item_index]
    test, train = test_m.tocsr(), train_m.tocsr()
    hits = []
    for u in np.unique(test.nonzero()[0]):
        seen = set(train.indices[train.indptr[u]:train.indptr[u + 1]])
        top = []
        for i in order:
            if i not in seen:
                top.append(i)
                if len(top) == k:
                    break
        bought = set(test.indices[test.indptr[u]:test.indptr[u + 1]])
        hits.append(len(bought.intersection(top)) / k)
    return round(float(np.mean(hits)), 4) if hits else None


def precision_future(M, start, days=VALID_DAYS, k=5):
    """precision@5 of a model on what its customers bought in (start, start + days], and of the best-seller
    baseline on exactly the same customers and buys. Returns (model p@5, customers scored, baseline p@5).
    Re-buys of items already in her training history are not counted (the model never shows them)."""
    start = ymd(start)
    fut = q(f"""
        SELECT DISTINCT CUSTOMER_ID, ARTICLE_ID FROM {TX}
        WHERE T_DAT > '{start}' AND T_DAT <= DATEADD('day', {int(days)}, '{start}')
    """)
    fut = fut[fut["CUSTOMER_ID"].isin(M["uid"]) & fut["ARTICLE_ID"].isin(M["iid"])]
    users = fut["CUSTOMER_ID"].unique()
    if len(users) == 0:
        return None, 0, None
    if len(users) > EVAL_USERS:
        users = np.random.default_rng(SEED).choice(users, EVAL_USERS, replace=False)
        fut = fut[fut["CUSTOMER_ID"].isin(set(users))]
    test_m, _ = M["dataset"].build_interactions(zip(fut["CUSTOMER_ID"], fut["ARTICLE_ID"]))
    test_m = test_m.tocsr()
    test_m = test_m - test_m.multiply(M["train_matrix"].tocsr() > 0)
    test_m.eliminate_zeros()
    test_m = test_m.tocoo()
    if test_m.nnz == 0:
        return None, 0, None
    p = precision_at_k(M["model"], test_m, train_interactions=M["train_matrix"], k=k,
                       user_features=M.get("user_features"), item_features=M.get("item_features"),
                       num_threads=os.cpu_count())
    base = baseline_precision(M["dataset"].mapping()[2], best_sellers(start), test_m, M["train_matrix"], k)
    return round(float(p.mean()), 4), int(len(p)), base


def validate(d, days=VALID_DAYS):
    """The model check: train both models on data up to (d - 15 days), measure them - and the best-seller
    baseline on the same customers - on the 15 days after."""
    d = ymd(d)
    dv = ymd(pd.Timestamp(d) - pd.Timedelta(days=days))
    pool = build_pool(dv, save=False)
    taste, basket = train_taste(dv, pool), train_basket(dv)
    tp, tn, tb = precision_future(taste, dv, days)
    bp, bn, bb = precision_future(basket, dv, days)
    out = dict(check_trained_to=dv, check_days=days, taste_p5=tp, taste_customers=tn, taste_base_p5=tb,
               basket_p5=bp, basket_customers=bn, basket_base_p5=bb)
    print(f"check {d} | trained to {dv}, tested on next {days} days | taste p@5 {tp} vs best sellers {tb} "
          f"({tn:,} customers) | basket p@5 {bp} vs best sellers {bb} ({bn:,} customers)")
    return out


GATE_MAX_DROP = 0.20             # the new model may be at most 20% worse than last month's


def gate(check, previous=None):
    """Decide if the new models may be used. Returns (status, reason).
    previous = last month's check result (dict with taste_p5 / basket_p5), None for the first month.
      FAIL -> keep last month's models: nothing could be measured, or a model is > 20% worse than last month
      PASS -> use the new models"""
    for m in ("taste", "basket"):
        if check.get(f"{m}_p5") is None:
            return "FAIL", f"no {m} precision measured (no customers in the check window)"
    if previous:
        for m in ("taste", "basket"):
            new, old = check[f"{m}_p5"], previous.get(f"{m}_p5")
            if old and new < (1 - GATE_MAX_DROP) * old:
                return "FAIL", f"{m} p@5 {new} is more than {int(GATE_MAX_DROP * 100)}% below last month ({old})"
        return "PASS", f"taste p@5 {check['taste_p5']} (last month {previous.get('taste_p5')}), " \
                       f"basket p@5 {check['basket_p5']} (last month {previous.get('basket_p5')})"
    return "PASS", f"first model: taste p@5 {check['taste_p5']}, basket p@5 {check['basket_p5']}"


def beats_baseline(check):
    """Does every model beat "show this week's best sellers" on the same customers? Returns (ok, reason).
    Not a gate rule on purpose: if this month's models lose, last month's (same data, same method) are no better,
    so there is nothing better to fall back to. A loss makes the training WARN + raises an alert for a person;
    the gate above keeps blocking real regressions. A baseline that could not be measured counts as no verdict."""
    behind = [f"{m} p@5 {check[f'{m}_p5']} <= best sellers {check[f'{m}_base_p5']}"
              for m in ("taste", "basket")
              if check.get(f"{m}_p5") is not None and check.get(f"{m}_base_p5") is not None
              and check[f"{m}_p5"] <= check[f"{m}_base_p5"]]
    return (False, "; ".join(behind)) if behind else (True, "")


# ======================================================================
# PART 8 - TRAIN + SAVE (on RETRAIN dates: the 1st of each month) and LOAD for a send
# Every training is written to NBO_MODEL_REGISTRY: check result, gate decision, which models a send must use.
# ======================================================================
def last_good(d):
    """The latest registry row before d whose gate PASSED (None if there is none yet)."""
    try:
        r = q(f"""SELECT * FROM {DB}.NBO_MODEL_REGISTRY
                  WHERE MODEL_DATE < '{ymd(d)}' AND GATE_STATUS = 'PASS'
                  ORDER BY MODEL_DATE DESC LIMIT 1""")
    except Exception:
        return None
    if r.empty:
        return None
    row = r.iloc[0].to_dict()
    return dict(taste_p5=row["TASTE_P5"], basket_p5=row["BASKET_P5"], model_used=row["MODEL_USED"])


def train_and_save(d):
    """1. model check (train to d-15, test on the 15 days after, best sellers measured on the same customers)
       2. gate vs last month
       PASS -> train both models on ALL data up to d, save taste_<d>.pkl / basket_<d>.pkl, use them
       FAIL -> train nothing, keep using last month's models (error if there are none)
       The registry row also says whether the models beat the best sellers (BEATS_BASELINE)."""
    t0, d = time.time(), ymd(d)
    check = validate(d)
    prev = last_good(d)
    status, reason = gate(check, prev)
    row = dict(MODEL_DATE=d, TRAINED_AT=str(pd.Timestamp.utcnow()), GATE_STATUS=status, GATE_REASON=reason,
               CHECK_TRAINED_TO=check["check_trained_to"], TASTE_P5=check["taste_p5"],
               TASTE_CHECK_CUSTOMERS=check["taste_customers"], BASKET_P5=check["basket_p5"],
               BASKET_CHECK_CUSTOMERS=check["basket_customers"],
               PREV_TASTE_P5=prev["taste_p5"] if prev else None, PREV_BASKET_P5=prev["basket_p5"] if prev else None,
               MODEL_USED=None, TASTE_CUSTOMERS=None, TASTE_ITEMS=None, BASKET_CUSTOMERS=None, BASKET_ITEMS=None)
    ok, behind = beats_baseline(check)
    row.update(TASTE_BASE_P5=check["taste_base_p5"], BASKET_BASE_P5=check["basket_base_p5"], BEATS_BASELINE=ok,
               BASELINE_NOTE=behind)
    if status == "PASS":
        pool = build_pool(d)
        taste, basket = train_taste(d, pool), train_basket(d)
        save_model(taste, f"taste_{d}")
        save_model(basket, f"basket_{d}")
        row.update(MODEL_USED=d, TASTE_CUSTOMERS=taste["n_customers"], TASTE_ITEMS=taste["n_items"],
                   BASKET_CUSTOMERS=basket["n_customers"], BASKET_ITEMS=basket["n_items"])
    elif prev:
        row["MODEL_USED"] = prev["model_used"]
    row["MINUTES"] = round((time.time() - t0) / 60, 1)
    row["CODE_VERSION"] = code_version()                   # which git commit trained these models

    db, schema = DB.split(".")
    session.sql(f"DELETE FROM {DB}.NBO_MODEL_REGISTRY WHERE MODEL_DATE = '{d}'").collect()
    session.write_pandas(pd.DataFrame([row]), "NBO_MODEL_REGISTRY", database=db, schema=schema,
                         auto_create_table=False)
    print(f"registry {d} | gate {status}: {reason} | models used: {row['MODEL_USED']}")
    if row["MODEL_USED"] is None:
        raise RuntimeError(f"Gate {status} and no earlier good model exists: {reason}")
    return row


def models_for(d):
    """The models a send on date d must use: its MODEL_DATE from the calendar -> MODEL_USED from the registry."""
    md = calendar_row(d)["MODEL_DATE"]
    r = q(f"SELECT MODEL_USED, GATE_STATUS FROM {DB}.NBO_MODEL_REGISTRY WHERE MODEL_DATE = '{md}'")
    if r.empty or pd.isna(r["MODEL_USED"].iloc[0]):
        raise RuntimeError(f"No usable model for {ymd(d)} (model date {md}): run training for {md} first.")
    used = ymd(r["MODEL_USED"].iloc[0])
    return load_model(f"taste_{used}"), load_model(f"basket_{used}"), used
