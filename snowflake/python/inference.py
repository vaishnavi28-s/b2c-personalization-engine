from __future__ import annotations

import hashlib

# ======================================================================
# inference.py - the "plating" part: fills the 6 slots for every customer of a send
#
#   1 Your style                    taste model, her top item
#   2 <occasion> (seasonal)         taste model, her top item among festival / theme / in-season items
#   3 Popular with people like you  basket model ("sales only")             fallback: #1 seller
#   4 Trending for you              her age group x her section, 2 x trend + 1 x match
#   5 Something new to try          taste top 50 -> new (type, colour) pair -> new type OR colour -> best left
#   6 Something new for <occasion>  same rule on the occasion items
#
# Rules for every slot: nothing she bought, no product twice in one email, a product in at most 20% of emails,
# at most 1 swimwear item, season rules (no coats in summer), no item without a photo, only items in the shop now.
# Audience: newsletter consent, max 1 email per 15 days, 5% holdout (picks are computed but never sent).
# Measurement: every email gets an EMAIL_ID and shows its 6 slots in a shuffled order (POSITION is saved),
# so clicks / buys can later be scored per slot without position bias.
#
#   Part 1  settings + audience       who gets an email (consent, frequency cap, holdout)
#   Part 2  shelf for this send       what may be shown (in stock, season rules, photo, occasion)
#   Part 3  her profile               what she bought, her section, her colours and types
#   Part 4  scoring + email rules     model scores, the per-email rules
#   Part 5  the 6 slots               slot by slot logic
#   Part 6  run a send                all customers, email id + shuffled order, quality numbers
# ======================================================================
import math

import numpy as np
import pandas as pd

import model_training as mt

SLOTS = {1: "Your style", 2: "{occ}", 3: "Popular with people like you", 4: "Trending for you",
         5: "Something new to try", 6: "Something new for {occ_lower}"}
N_SLOTS        = 6
CAP_SHARE      = 0.20            # one product in at most 20% of the emails of a send
SWIM_MAX       = 1               # at most 1 swimwear item per email
INSP_TOP_K     = 50              # slots 5/6 look at her top 50
THIN_HISTORY   = 3               # < 3 bought items -> slots 5/6 start with OR
HISTORY_DAYS   = 365             # what she bought (blocking + novelty) = last 12 months
CONTACT_DAYS   = 14              # max 1 email per 15 days: skip anyone emailed in the last 14 days
HOLDOUT_SHARE  = 0.05            # 5% of customers never get an email (to measure real lift)
CONSENT_ONLY   = True            # only customers who agreed to the newsletter
ADULT          = ("Women", "Men", "Young")
SCORE_CHUNK    = 500             # customers scored per matrix multiply (memory)


# ======================================================================
# PART 1 - AUDIENCE: who gets an email
# ======================================================================
def is_holdout(customer_id):
    """Stable 5% holdout: the same customer is always in the same group (hash of the id, not random per send)."""
    h = int(hashlib.md5(str(customer_id).encode()).hexdigest()[:8], 16)
    return (h % 10000) < HOLDOUT_SHARE * 10000


def pick_customers(d, taste, n):
    """Customers for this send: in the taste model, newsletter consent, not emailed in the last 14 days."""
    d = mt.ymd(d)
    pool = set(taste["uid"])
    if CONSENT_ONLY:
        ok = mt.q(f"SELECT CUSTOMER_ID FROM {mt.CUS} WHERE UPPER(COALESCE(FASHION_NEWS_FREQUENCY, 'NONE')) <> 'NONE'")
        pool &= set(ok["CUSTOMER_ID"])
    recent = mt.q(f"""SELECT DISTINCT CUSTOMER_ID FROM {mt.DB}.NBO_RECOMMENDATIONS
                      WHERE GRP = 'EMAIL' AND RUN_DATE < '{d}'
                        AND RUN_DATE >= TO_VARCHAR(DATEADD('day', -{CONTACT_DAYS}, '{d}'::DATE), 'YYYY-MM-DD')""")
    pool -= set(recent["CUSTOMER_ID"])
    pool = sorted(pool)
    rng = np.random.default_rng(int(d.replace("-", "")))
    return [str(c) for c in rng.choice(pool, size=min(int(n), len(pool)), replace=False)] if pool else []


# ======================================================================
# PART 2 - SHELF FOR THIS SEND: what may be shown
# ======================================================================
def load_no_image():
    """Articles without a product photo (an email card needs a photo)."""
    r = mt.q(f"SELECT ARTICLE_ID FROM {mt.DB}.NBO_NO_IMAGE")
    return set(r["ARTICLE_ID"].astype(str).str.zfill(10))


def prepare_send(d, taste, basket):
    """Everything the slots choose from, after the hard rules (in stock, season rules, photo)."""
    d = mt.ymd(d)
    month = pd.Timestamp(d).month
    occ = mt.build_occasion(d)
    pool = mt.build_pool(d)
    rules, no_image = mt.load_season_rules(), load_no_image()

    stock = mt.q(f"""
        SELECT t.ARTICLE_ID, COUNT(*) AS SALES_28D
        FROM {mt.TX} t JOIN {mt.ART} a ON a.ARTICLE_ID = t.ARTICLE_ID
        WHERE t.T_DAT > DATEADD('day', -{mt.POOL_DAYS}, '{d}') AND t.T_DAT <= '{d}'
          AND a.PRODUCT_GROUP_NAME IN {mt.GARMENTS}
        GROUP BY t.ARTICLE_ID
    """)
    sales = dict(zip(stock["ARTICLE_ID"], stock["SALES_28D"].astype(float)))

    def allowed(df):
        df = df.copy()
        keep = (df["ARTICLE_ID"].isin(sales)
                & ~mt.season_blocked(df, month, rules)
                & ~df["ARTICLE_ID"].astype(str).str.zfill(10).isin(no_image))
        df = df[np.asarray(keep, dtype=bool)].reset_index(drop=True)
        df["SALES_28D"] = df["ARTICLE_ID"].map(sales).astype(float)
        return df

    # taste candidates: model items whose product is on today's shelf
    shelf = taste["items"][taste["items"]["PRODUCT_CODE"].isin(set(pool["PRODUCT_CODE"]))]
    shelf = allowed(shelf)
    shelf["IDX"] = shelf["ARTICLE_ID"].map(taste["iid"]).astype(int)
    shelf["OCC"] = np.asarray(mt.occasion_mask(shelf, occ), dtype=bool)
    shelf["BOOST"] = shelf["PATTERN"].eq(occ["boost_pattern"]) if occ["boost_pattern"] else False

    bask = allowed(basket["items"])
    bask["IDX"] = bask["ARTICLE_ID"].map(basket["iid"]).astype(int)

    trending = {k: allowed(v) for k, v in mt.build_trending(d).items()}
    trending = {k: v for k, v in trending.items() if len(v)}

    names = dict(occ=occ["occasion"], occ_lower=str(occ["occasion"]).lower())
    labels = {i: s.format(**names) for i, s in SLOTS.items()}
    print(f"send {d} | shelf {len(shelf):,} | occasion items {int(shelf['OCC'].sum()):,} ({occ['source']}) | "
          f"basket {len(bask)} | trending lists {len(trending)}")
    return dict(date=d, month=month, occ=occ, pool=pool, shelf=shelf, basket=bask,
                trending=trending, labels=labels)


# ======================================================================
# PART 3 - HER PROFILE: what she bought
# ======================================================================
def load_history(d, custs):
    """Her garment purchases in the last 12 months (blocking + novelty + trending match)."""
    d = mt.ymd(d)
    db, schema = mt.DB.split(".")
    mt.session.write_pandas(pd.DataFrame({"CUSTOMER_ID": list(custs)}), "NBO_TMP_CUST", database=db, schema=schema,
                            auto_create_table=True, overwrite=True, table_type="temporary")
    h = mt.q(f"""
        SELECT t.CUSTOMER_ID, t.ARTICLE_ID, a.PRODUCT_CODE, a.PRODUCT_TYPE_NAME AS PTYPE,
               a.PERCEIVED_COLOUR_MASTER_NAME AS COLOUR, a.GRAPHICAL_APPEARANCE_NAME AS PATTERN,
               a.INDEX_GROUP_NAME AS DEPT, a.SECTION_NAME AS SECTION
        FROM {mt.TX} t
        JOIN {mt.ART} a ON a.ARTICLE_ID = t.ARTICLE_ID
        JOIN {mt.DB}.NBO_TMP_CUST x ON x.CUSTOMER_ID = t.CUSTOMER_ID
        WHERE t.T_DAT <= '{d}' AND t.T_DAT > DATEADD('day', -{HISTORY_DAYS}, '{d}')
          AND a.PRODUCT_GROUP_NAME IN {mt.GARMENTS}
    """)
    h["SEC"] = mt.section_of(h["DEPT"], h["SECTION"])
    return {c: g for c, g in h.groupby("CUSTOMER_ID")}


def her_profile(hist):
    """What we know about her from her purchases."""
    if hist is None or hist.empty:
        return dict(bought=set(), pairs=set(), types=set(), colours=set(), patterns=set(),
                    section="Women", section_default=True, n_items=0)
    adult = hist[hist["SEC"].isin(ADULT)]
    if len(adult):
        section, default = adult["SEC"].value_counts().idxmax(), False
    else:
        section, default = "Women", True            # e.g. only baby clothes: most H&M buyers are women
    return dict(bought=set(hist["ARTICLE_ID"]),
                pairs=set(zip(hist["PTYPE"], hist["COLOUR"])),
                types=set(hist["PTYPE"]), colours=set(hist["COLOUR"]), patterns=set(hist["PATTERN"]),
                section=section, section_default=default, n_items=hist["ARTICLE_ID"].nunique())


# ======================================================================
# PART 4 - SCORING + EMAIL RULES
# ======================================================================
def taste_scores(taste, custs):
    """Taste-model scores for these customers x all model items: bias_u + bias_i + u . i."""
    ub, ue = taste["model"].get_user_representations(taste["user_features"])
    ib, ie = taste["model"].get_item_representations(taste["item_features"])
    rows = np.array([taste["uid"][c] for c in custs])
    for s in range(0, len(rows), SCORE_CHUNK):
        r = rows[s:s + SCORE_CHUNK]
        yield custs[s:s + SCORE_CHUNK], ub[r][:, None] + ib[None, :] + ue[r] @ ie.T


def basket_scores(basket, cid):
    if cid not in basket["uid"]:
        return None
    if "_reps" not in basket:                       # computed once per send
        basket["_reps"] = (basket["model"].get_user_representations(), basket["model"].get_item_representations())
    (ub, ue), (ib, ie) = basket["_reps"]
    u = basket["uid"][cid]
    return ub[u] + ib + ie @ ue[u]


# ---------------- the per-email rules ----------------
class Email:
    """Collects one customer's 6 picks and enforces the email-level rules."""

    def __init__(self, prof, shown, cap):
        self.prof, self.shown, self.cap = prof, shown, cap
        self.products, self.swim, self.picks = set(), 0, {}

    def ok(self, r):
        return (r.ARTICLE_ID not in self.prof["bought"]
                and r.PRODUCT_CODE not in self.products
                and self.shown.get(r.PRODUCT_CODE, 0) < self.cap
                and not (r.PGROUP == "Swimwear" and self.swim >= SWIM_MAX))

    def first(self, df):
        for r in df.itertuples():
            if self.ok(r):
                return r
        return None

    def take(self, slot, r, source, rule, score=None):
        if r is None:
            return
        self.products.add(r.PRODUCT_CODE)
        self.swim += int(r.PGROUP == "Swimwear")
        self.picks[slot] = dict(ARTICLE_ID=r.ARTICLE_ID, PRODUCT_CODE=r.PRODUCT_CODE, PROD_NAME=r.PROD_NAME,
                                DEPARTMENT=r.DEPT, SECTION=r.SEC, TYPE=r.PTYPE, GROUP_NAME=r.PGROUP,
                                COLOUR=r.COLOUR, PATTERN=r.PATTERN, SOURCE=source, RULE=rule,
                                SCORE=None if score is None or pd.isna(score) else round(float(score), 4))


# ======================================================================
# PART 5 - THE 6 SLOTS
# ======================================================================
def novelty(cands, prof, mode):
    """Inspiration filter. AND: the (type, colour) PAIR is new to her. OR: the type OR the colour is new."""
    if mode == "AND":
        m = ~pd.Series(list(zip(cands["PTYPE"], cands["COLOUR"])), index=cands.index).isin(prof["pairs"])
    else:
        m = ~cands["PTYPE"].isin(prof["types"]) | ~cands["COLOUR"].isin(prof["colours"])
    return cands[np.asarray(m, dtype=bool)]


def pick_new(e, ranked, prof):
    """Slots 5/6: her top 50 still allowed -> AND -> OR -> best left (step-by-step relaxation)."""
    keep = []
    for i, r in zip(ranked.index, ranked.itertuples()):  # her top 50 that the email rules still allow
        if e.ok(r):
            keep.append(i)
            if len(keep) == INSP_TOP_K:
                break
    if not keep:
        return None, None
    top = ranked.loc[keep]
    start = "AND" if prof["n_items"] >= THIN_HISTORY else "OR"
    for mode in (["AND", "OR"] if start == "AND" else ["OR"]):
        f = novelty(top, prof, mode)
        if len(f):
            return f.iloc[0], mode
    return top.iloc[0], "best"


def trending_pick(e, S, age, prof):
    """Slot 4: her age group x her section; 2 x trend (scaled 0-1) + 1 x match (type 1, colour 0.5, pattern 0.5)."""
    tries = [(age, prof["section"]), (age, "all"), ("all", prof["section"]), ("all", "all")]
    for key in tries:
        lst = S["trending"].get(key)
        if lst is None or lst.empty:
            continue
        t = lst.copy()
        t["MATCH"] = (t["PTYPE"].isin(prof["types"]) * 1.0 + t["COLOUR"].isin(prof["colours"]) * 0.5
                      + t["PATTERN"].isin(prof["patterns"]) * 0.5)
        t["FINAL"] = 2.0 * t["TREND_SCORE"] / max(t["TREND_SCORE"].max(), 1e-9) + 1.0 * t["MATCH"]
        r = e.first(t.sort_values("FINAL", ascending=False))
        if r is not None:
            return r, f"list {key[0]}/{key[1]}", r.FINAL, key == tries[0]
    return None, None, None, False


def email_for(cid, scores, S, taste, basket, prof, age, shown, cap):
    """The 6 picks for one customer, in slot order (each slot skips what earlier slots took)."""
    e = Email(prof, shown, cap)
    shelf, occ = S["shelf"], S["occ"]
    ranked = shelf.assign(SCORE=scores[shelf["IDX"].values]).sort_values("SCORE", ascending=False)
    occ_ranked = ranked[ranked["OCC"].values]
    by_sales = shelf.sort_values("SALES_28D", ascending=False)

    # 1 Your style
    r = e.first(ranked)
    if r is not None:
        e.take(1, r, "model", "taste top", r.SCORE)
    else:
        e.take(1, e.first(by_sales), "fallback", "top seller")

    # 2 Occasion: her best occasion item (festive pattern first among her top 50)
    top = occ_ranked.head(INSP_TOP_K)
    if occ["boost_pattern"] is not None and len(top):
        top = top.sort_values(["BOOST", "SCORE"], ascending=[False, False])
    r = e.first(top) or e.first(occ_ranked)
    if r is not None:
        e.take(2, r, "model", occ["source"], r.SCORE)
    else:
        e.take(2, e.first(by_sales[by_sales["OCC"].values]) or e.first(by_sales), "fallback", "top seasonal seller")

    # 3 Popular with people like you (basket model)
    bs = basket_scores(basket, cid)
    if bs is not None:
        b = S["basket"].assign(SCORE=bs[S["basket"]["IDX"].values]).sort_values("SCORE", ascending=False)
        r = e.first(b)
        if r is not None:
            e.take(3, r, "model", "basket", r.SCORE)
    if 3 not in e.picks:
        e.take(3, e.first(S["basket"].sort_values("SALES", ascending=False)), "fallback", "#1 seller")

    # 4 Trending for you
    r, rule, sc, primary = trending_pick(e, S, age, prof)
    e.take(4, r, "model" if primary else "fallback", rule, sc)

    # 5 Something new to try / 6 Something new for the occasion
    for slot, base in ((5, ranked), (6, occ_ranked)):
        r, mode = pick_new(e, base, prof)
        if r is not None:
            e.take(slot, r, "model", mode, r.SCORE)
        else:
            pool = by_sales[by_sales["OCC"].values] if slot == 6 else by_sales
            e.take(slot, e.first(pool) or e.first(by_sales), "fallback", "top seller")
    return e.picks


# ======================================================================
# PART 6 - RUN A SEND: all customers, email id + shuffled order, quality numbers
# ======================================================================
def email_id(d, cid):
    """One id per email: send date + customer (the same email always gets the same id)."""
    return f"E{d.replace('-', '')}_{cid}"


def display_order(eid):
    """Shuffled order of the 6 slots in this email (position bias): slot -> position 1..6.
    Seeded by the email id, so a re-run shows the same order."""
    rng = np.random.default_rng(int(hashlib.md5(eid.encode()).hexdigest()[:8], 16))
    shown = rng.permutation(np.arange(1, N_SLOTS + 1))          # e.g. [4, 1, 6, 2, 5, 3]: slot 4 shown first
    return {int(slot): pos for pos, slot in enumerate(shown, start=1)}


def run_send(d, taste, basket, n_customers):
    """Fill the slots for one send date.
    Returns: wide rows (1 per customer), item rows (1 per pick), quality numbers, first errors, occasion info."""
    d = mt.ymd(d)
    S = prepare_send(d, taste, basket)
    custs = pick_customers(d, taste, n_customers)
    if not custs:
        raise ValueError("No eligible customers for this send.")
    rng = np.random.default_rng(int(d.replace("-", "")) + 1)
    custs = list(rng.permutation(custs))                 # random order: the 20% cap is fair to everyone
    hist = load_history(d, custs)
    age = dict(zip(taste["customers"]["CUSTOMER_ID"], taste["customers"]["AGE_GROUP"]))
    grp = {c: ("HOLDOUT" if is_holdout(c) else "EMAIL") for c in custs}
    n_email = sum(g == "EMAIL" for g in grp.values())
    cap = max(1, math.ceil(CAP_SHARE * n_email))
    shown, wide, items, failed = {}, [], [], []

    for chunk, scores in taste_scores(taste, custs):
        for cid, sc in zip(chunk, scores):
            try:
                prof = her_profile(hist.get(cid))
                picks = email_for(cid, sc, S, taste, basket, prof, age.get(cid, "unknown"), shown, cap)
                if grp[cid] == "EMAIL":
                    for p in picks.values():
                        shown[p["PRODUCT_CODE"]] = shown.get(p["PRODUCT_CODE"], 0) + 1
                eid = email_id(d, str(cid))
                order = display_order(eid)
                row = dict(RUN_DATE=d, EMAIL_ID=eid, CUSTOMER_ID=str(cid), GRP=grp[cid], AGE_GROUP=age.get(cid, "unknown"),
                           SECTION=prof["section"], SECTION_DEFAULTED=bool(prof["section_default"]),
                           N_HISTORY=int(prof["n_items"]), INSP_MODE="AND" if prof["n_items"] >= THIN_HISTORY else "OR",
                           IN_BASKET_MODEL=bool(cid in basket["uid"]), OCCASION=S["occ"]["occasion"],
                           MODEL_DATE=taste["date"])
                for i in range(1, N_SLOTS + 1):
                    row[f"SLOT_{i}_ARTICLE"] = str(picks[i]["ARTICLE_ID"]) if i in picks else ""
                wide.append(row)
                for i, p in sorted(picks.items()):
                    items.append(dict(RUN_DATE=d, EMAIL_ID=eid, CUSTOMER_ID=str(cid), GRP=grp[cid], POS=i,
                                      POSITION=int(order[i]), SLOT=S["labels"][i],
                                      OCCASION=S["occ"]["occasion"], MODEL_DATE=taste["date"], **p))
            except Exception as ex:
                failed.append(f"{cid}: {ex}")
    metrics = quality_metrics(pd.DataFrame(wide), pd.DataFrame(items), S, cap)
    metrics["customers_failed"] = len(failed)
    info = dict(occasion=S["occ"]["occasion"], occasion_source=S["occ"]["source"], cap=cap)
    return wide, items, metrics, failed[:5], info


# ---------------- quality numbers (output checks + monitoring) ----------------
def quality_metrics(wide, items, S, cap):
    """Numbers about this send's emails. Hard failures are checked by the orchestrator."""
    m = {}
    em = wide[wide["GRP"] == "EMAIL"] if len(wide) else wide
    it = items[items["GRP"] == "EMAIL"] if len(items) else items
    m["customers"] = len(wide)
    m["customers_email"] = len(em)
    m["customers_holdout"] = int((wide["GRP"] == "HOLDOUT").sum()) if len(wide) else 0
    if not len(em):
        return m
    filled = (em[[f"SLOT_{i}_ARTICLE" for i in range(1, N_SLOTS + 1)]] != "").sum(axis=1)
    m["emails_complete_share"] = round(float((filled == N_SLOTS).mean()), 4)
    for i in range(1, N_SLOTS + 1):
        s = it[it["POS"] == i]
        m[f"slot{i}_fill_rate"] = round(len(s) / len(em), 4)
        m[f"slot{i}_fallback_rate"] = round(float((s["SOURCE"] == "fallback").mean()), 4) if len(s) else 1.0
    counts = it["PRODUCT_CODE"].value_counts()
    m["distinct_articles"] = int(it["ARTICLE_ID"].nunique())
    m["distinct_products"] = int(it["PRODUCT_CODE"].nunique())
    m["top_product_share"] = round(float(counts.iloc[0] / len(em)), 4) if len(counts) else 0.0
    m["cap"] = int(cap)
    m["cap_broken"] = int(bool(len(counts)) and counts.iloc[0] > cap)
    m["cold_start_share"] = round(float((~em["IN_BASKET_MODEL"]).mean()), 4)
    m["section_default_share"] = round(float(em["SECTION_DEFAULTED"].mean()), 4)
    m["insp_mode_and_share"] = round(float((em["INSP_MODE"] == "AND").mean()), 4)
    for slot in (5, 6):
        s = it[it["POS"] == slot]
        for rule in ("AND", "OR", "best"):
            m[f"slot{slot}_{rule.lower()}_share"] = round(float((s["RULE"] == rule).mean()), 4) if len(s) else 0.0
    m["slot4_own_list_share"] = round(float((it[it["POS"] == 4]["SOURCE"] == "model").mean()), 4)
    m["swim_max_per_email"] = int(it[it["GROUP_NAME"] == "Swimwear"].groupby("CUSTOMER_ID").size().max()) \
        if (it["GROUP_NAME"] == "Swimwear").any() else 0
    return m


HARD_CHECKS = {   # metric: (rule, limit) - a failure stops the send
    "emails_complete_share": (">=", 0.99),
    "cap_broken": ("==", 0),
    "swim_max_per_email": ("<=", SWIM_MAX),
    "customers_failed": ("==", 0),
}


def hard_check(metrics):
    """Returns a list of failed hard checks (empty = OK)."""
    bad = []
    for k, (op, lim) in HARD_CHECKS.items():
        v = metrics.get(k)
        if v is None:
            continue
        ok = {">=": v >= lim, "<=": v <= lim, "==": v == lim}[op]
        if not ok:
            bad.append(f"{k}={v} (needs {op} {lim})")
    return bad
