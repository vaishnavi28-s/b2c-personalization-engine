"""
The production rules, checked on what the real pipeline wrote to Snowflake.
Each test reads the output tables with SQL; tests that need extra data create it and remove it again.
"""
import pandas as pd
import pytest

pytestmark = pytest.mark.integration


# ---------------- the run itself ----------------
def test_training_and_reuse(chain, dates):
    first, second = dates[0], dates[-1]
    assert chain[first]["train"]["status"] in ("SUCCESS", "WARN")            # WARN = gate failed, old models kept
    if second != first:
        assert chain[second]["train"]["status"] == "SKIPPED"
        assert chain[second]["recommend"]["models_used"] == chain[first]["recommend"]["models_used"]


def test_registry_row_is_complete(snow, chain, dates, deployed_version):
    r = snow.df("SELECT * FROM NBO_MODEL_REGISTRY WHERE MODEL_DATE = %s", (dates[0],))
    assert len(r) == 1
    row = r.iloc[0]
    assert row["GATE_STATUS"] in ("PASS", "FAIL") and row["GATE_REASON"]
    assert row["TASTE_P5"] is not None and row["BASKET_P5"] is not None
    assert row["TASTE_BASE_P5"] is not None and row["BASKET_BASE_P5"] is not None      # best sellers measured too
    assert row["BEATS_BASELINE"] in (True, False)
    assert row["CODE_VERSION"] == deployed_version


def test_every_step_is_logged_with_the_deployed_version(snow, chain, dates, deployed_version):
    log = snow.df("""SELECT PROCEDURE_NAME, STATUS, CODE_VERSION FROM NBO_RUN_LOG
                     WHERE RUN_DATE IN (%s, %s)
                     QUALIFY ROW_NUMBER() OVER (PARTITION BY PROCEDURE_NAME, RUN_DATE ORDER BY START_TIME DESC) = 1""",
                  (dates[0], dates[-1]))
    assert {"NBO_DATA_CHECKS", "NBO_TRAIN", "NBO_RECOMMEND", "NBO_RENDER_EMAILS", "NBO_EVENTS_SENT"} \
        <= set(log["PROCEDURE_NAME"])
    assert (log["CODE_VERSION"] == deployed_version).all()
    assert not (log["STATUS"] == "FAILED").any()


def test_data_checks_ran_and_none_failed(snow, chain, dates):
    c = snow.df("SELECT CHECK_NAME, STATUS FROM NBO_DATA_CHECKS WHERE RUN_DATE = %s", (dates[0],))
    assert {"required_columns_missing", "fresh_purchases_last_3_days", "empty_ids_last_7_days", "bad_price_share",
            "purchases_dated_after_today", "unknown_article_share", "unknown_customer_share",
            "articles_missing_attributes_share", "volume_change_vs_4_weeks", "repeated_rows_shift"} \
        == set(c["CHECK_NAME"])
    assert not (c["STATUS"] == "FAIL").any()


# ---------------- what is in the emails ----------------
def test_every_email_has_6_cards_in_6_positions(snow, chain, dates):
    g = snow.df("""SELECT EMAIL_ID, COUNT(*) N, COUNT(DISTINCT POS) S, COUNT(DISTINCT POSITION) P,
                          MIN(POSITION) LO, MAX(POSITION) HI
                   FROM NBO_RECOMMENDATION_ITEMS WHERE RUN_DATE IN (%s, %s) GROUP BY 1""", (dates[0], dates[-1]))
    assert len(g) > 0
    assert (g["N"] == 6).all() and (g["S"] == 6).all() and (g["P"] == 6).all()
    assert (g["LO"] == 1).all() and (g["HI"] == 6).all()


def test_display_order_is_shuffled(snow, chain, dates):
    """Slot 1 must not always be shown first - otherwise position effects hide slot effects."""
    p = snow.df("SELECT COUNT(DISTINCT POSITION) N FROM NBO_RECOMMENDATION_ITEMS WHERE RUN_DATE = %s AND POS = 1",
                (dates[0],))
    assert p["N"].iloc[0] == 6


def test_no_product_twice_in_an_email(snow, chain, dates):
    bad = snow.df("""SELECT COUNT(*) N FROM (
                       SELECT EMAIL_ID FROM NBO_RECOMMENDATION_ITEMS WHERE RUN_DATE IN (%s, %s)
                       GROUP BY EMAIL_ID HAVING COUNT(*) <> COUNT(DISTINCT PRODUCT_CODE))""", (dates[0], dates[-1]))
    assert bad["N"].iloc[0] == 0


def test_nothing_she_bought_in_the_last_year(snow, chain, dates):
    bad = snow.df("""SELECT COUNT(*) N FROM NBO_RECOMMENDATION_ITEMS i
                     JOIN NBO_REBUILD.HM_ODS.HM_NBO_DWH_F_TRANSACTION t
                       ON t.CUSTOMER_ID = i.CUSTOMER_ID AND t.ARTICLE_ID = i.ARTICLE_ID
                      AND t.T_DAT <= i.RUN_DATE::DATE AND t.T_DAT > DATEADD('day', -365, i.RUN_DATE::DATE)
                     WHERE i.RUN_DATE IN (%s, %s)""", (dates[0], dates[-1]))
    assert bad["N"].iloc[0] == 0


def test_only_items_with_a_photo(snow, chain, dates):
    bad = snow.df("""SELECT COUNT(*) N FROM NBO_RECOMMENDATION_ITEMS i
                     JOIN NBO_NO_IMAGE x ON LPAD(x.ARTICLE_ID, 10, '0') = LPAD(i.ARTICLE_ID, 10, '0')
                     WHERE i.RUN_DATE IN (%s, %s)""", (dates[0], dates[-1]))
    assert bad["N"].iloc[0] == 0


def test_season_rules_hold(snow, chain, dates):
    bad = snow.df("""SELECT COUNT(*) N FROM NBO_RECOMMENDATION_ITEMS i
                     JOIN NBO_SEASON_RULES r
                       ON r.PRODUCT_GROUP = i.GROUP_NAME AND (r.PRODUCT_TYPE IS NULL OR r.PRODUCT_TYPE = i.TYPE)
                      AND ARRAY_CONTAINS(TO_VARCHAR(MONTH(i.RUN_DATE::DATE))::VARIANT,
                                         SPLIT(REPLACE(r.BLOCKED_MONTHS, ' ', ''), ','))
                     WHERE i.RUN_DATE IN (%s, %s)""", (dates[0], dates[-1]))
    assert bad["N"].iloc[0] == 0


def test_cap_and_swimwear(snow, chain, dates):
    m = snow.df("""SELECT RUN_DATE, METRIC, VALUE FROM NBO_RUN_METRICS
                   WHERE RUN_DATE IN (%s, %s) AND METRIC IN ('cap_broken', 'swim_max_per_email',
                         'emails_complete_share', 'customers_failed')""", (dates[0], dates[-1]))
    v = m.pivot(index="RUN_DATE", columns="METRIC", values="VALUE")
    assert (v["cap_broken"] == 0).all() and (v["swim_max_per_email"] <= 1).all()
    assert (v["emails_complete_share"] >= 0.99).all() and (v["customers_failed"] == 0).all()
    # independent recount from the stored cards (not trusting the metric)
    over = snow.df("""WITH e AS (SELECT RUN_DATE, COUNT(DISTINCT EMAIL_ID) N FROM NBO_RECOMMENDATION_ITEMS
                                 WHERE GRP = 'EMAIL' AND RUN_DATE IN (%s, %s) GROUP BY 1),
                          p AS (SELECT RUN_DATE, PRODUCT_CODE, COUNT(*) N FROM NBO_RECOMMENDATION_ITEMS
                                 WHERE GRP = 'EMAIL' AND RUN_DATE IN (%s, %s) GROUP BY 1, 2)
                     SELECT COUNT(*) N FROM p JOIN e USING (RUN_DATE) WHERE p.N > CEIL(0.2 * e.N)""",
                   (dates[0], dates[-1], dates[0], dates[-1]))
    assert over["N"].iloc[0] == 0


def test_slot_4_never_shows_kids_items(snow, chain, dates):
    n = snow.df("SELECT COUNT(*) N FROM NBO_RECOMMENDATION_ITEMS WHERE RUN_DATE IN (%s, %s) AND POS = 4 "
                "AND SECTION = 'Kids'", (dates[0], dates[-1]))
    assert n["N"].iloc[0] == 0


# ---------------- who gets an email ----------------
def test_holdout_never_gets_an_email(snow, chain, dates):
    n = snow.df("""SELECT COUNT(*) N FROM NBO_EMAIL_OUTBOX o JOIN NBO_RECOMMENDATIONS r ON r.EMAIL_ID = o.EMAIL_ID
                   WHERE r.GRP = 'HOLDOUT'""")
    assert n["N"].iloc[0] == 0
    s = snow.df("SELECT AVG(IFF(GRP = 'HOLDOUT', 1, 0)) S FROM NBO_RECOMMENDATIONS WHERE RUN_DATE IN (%s, %s)",
                (dates[0], dates[-1]))
    assert 0.015 < float(s["S"].iloc[0]) < 0.09                       # 5% target, small sample


def test_only_newsletter_customers(snow, chain, dates):
    n = snow.df("""SELECT COUNT(*) N FROM NBO_RECOMMENDATIONS r
                   JOIN NBO_REBUILD.HM_ODS.HM_NBO_DWH_D_CUSTOMER c ON c.CUSTOMER_ID = r.CUSTOMER_ID
                   WHERE r.RUN_DATE IN (%s, %s) AND UPPER(COALESCE(c.FASHION_NEWS_FREQUENCY, 'NONE')) = 'NONE'""",
                (dates[0], dates[-1]))
    assert n["N"].iloc[0] == 0


def test_nobody_is_emailed_twice_within_14_days(snow, chain, dates):
    """An extra send 9 days later must skip everyone emailed on the first date. The probe is removed afterwards."""
    probe = (pd.Timestamp(dates[0]) + pd.Timedelta(days=9)).strftime("%Y-%m-%d")
    try:
        r = snow.call("NBO_RECOMMEND", probe, 500)
        if r["status"] != "SUCCESS":
            pytest.fail(f"probe send failed: {r}")
        both = snow.df("""SELECT COUNT(*) N FROM NBO_RECOMMENDATIONS a JOIN NBO_RECOMMENDATIONS b
                            ON a.CUSTOMER_ID = b.CUSTOMER_ID
                          WHERE a.RUN_DATE = %s AND b.RUN_DATE = %s AND a.GRP = 'EMAIL' AND b.GRP = 'EMAIL'""",
                       (dates[0], probe))
        assert both["N"].iloc[0] == 0
    finally:
        for t in ("NBO_RECOMMENDATIONS", "NBO_RECOMMENDATION_ITEMS", "NBO_RUN_METRICS", "NBO_RUN_LOG"):
            snow.run(f"DELETE FROM {t} WHERE RUN_DATE = %s", (probe,))
        snow.run("DELETE FROM NBO_POOL_BY_DATE WHERE POOL_DATE = %s", (probe,))


def test_rerun_replaces_instead_of_duplicating(snow, chain, dates):
    d = dates[-1]
    count = "SELECT COUNT(*) N, COUNT(DISTINCT EMAIL_ID) E FROM NBO_RECOMMENDATION_ITEMS WHERE RUN_DATE = %s"
    before = snow.df(count, (d,)).iloc[0]
    assert snow.call("NBO_RECOMMEND", d, 500)["status"] == "SUCCESS"
    after = snow.df(count, (d,)).iloc[0]
    assert (before["N"], before["E"]) == (after["N"], after["E"])
    assert snow.call("NBO_RENDER_EMAILS", d)["status"] == "SUCCESS"


# ---------------- emails, events, evaluation ----------------
def test_emails_are_complete_and_plain(snow, chain, dates):
    o = snow.df("SELECT HTML FROM NBO_EMAIL_OUTBOX WHERE STATUS = 'READY' AND RUN_DATE = %s", (dates[0],))
    if o.empty:
        pytest.skip("no active row in NBO_EMAIL_RECIPIENTS: no READY email to inspect")
    html = o["HTML"].iloc[0]
    assert html.count('<td width="50%" valign="top"') == 6                    # 6 product cards
    assert "<a " not in html and 'width="1" height="1"' not in html           # no links, no tracking pixel
    dry = snow.df("SELECT COUNT(*) N FROM NBO_EMAIL_OUTBOX WHERE STATUS = 'DRY_RUN' AND RUN_DATE = %s", (dates[0],))
    emailed = snow.df("SELECT COUNT(*) N FROM NBO_RECOMMENDATIONS WHERE GRP = 'EMAIL' AND RUN_DATE = %s",
                      (dates[0],))
    assert dry["N"].iloc[0] == emailed["N"].iloc[0]


def test_sent_events_match_the_cards(snow, chain, dates):
    s = snow.df("""SELECT (SELECT COUNT(*) FROM NBO_EMAIL_EVENTS WHERE RUN_DATE = %s AND EVENT_TYPE = 'sent') SENT,
                          (SELECT COUNT(*) FROM NBO_RECOMMENDATION_ITEMS WHERE RUN_DATE = %s AND GRP = 'EMAIL') CARDS""",
                (dates[0], dates[0]))
    assert s["SENT"].iloc[0] == s["CARDS"].iloc[0] > 0


def test_render_is_blocked_without_a_successful_recommend(snow, chain):
    r = snow.call("NBO_RENDER_EMAILS", "2019-10-05")      # never recommended
    assert r["status"] == "FAILED" and "blocked" in r
    snow.run("DELETE FROM NBO_RUN_LOG WHERE RUN_DATE = '2019-10-05'")


def test_errors_are_logged_not_lost(snow, chain):
    r = snow.call("NBO_EVENTS", "nonsense", None)
    assert r["status"] == "FAILED" and "unknown MODE" in r["error"]
    log = snow.df("""SELECT STATUS, ERROR_MESSAGE FROM NBO_RUN_LOG WHERE PROCEDURE_NAME = 'NBO_EVENTS_NONSENSE'
                     ORDER BY START_TIME DESC LIMIT 1""")
    assert log["STATUS"].iloc[0] == "FAILED" and "unknown MODE" in log["ERROR_MESSAGE"].iloc[0]
    snow.run("DELETE FROM NBO_RUN_LOG WHERE PROCEDURE_NAME = 'NBO_EVENTS_NONSENSE'")


def test_evaluation_scores_buys_from_the_purchase_data(snow, chain, dates):
    """The real evaluation of the send: buys within 90 days per slot / position / email, emailed vs holdout."""
    d = dates[0]
    e = snow.call("NBO_EVALUATE", d, 90)
    if e["status"] == "PENDING" and d > "2020-06-23":      # the data ends 2020-09-22: no full 90 days after d yet
        snow.run("DELETE FROM NBO_RUN_LOG WHERE PROCEDURE_NAME = 'NBO_EVALUATE' AND RUN_DATE = %s AND STATUS = 'PENDING'",
                 (d,))
        pytest.skip(f"{d}: the purchase data does not cover 90 days after it (rehearsal of a late date)")
    assert e["status"] == "SUCCESS" and e["emails"] > 0 and e["holdout_customers"] > 0
    res = snow.df("SELECT GROUP_BY, GROUP_KEY, METRIC, VALUE, CI_LOW, CI_HIGH FROM NBO_EVAL_RESULTS "
                  "WHERE RUN_DATE = %s", (d,))
    assert {"slot", "position", "email", "holdout", "holdout_pooled"} == set(res["GROUP_BY"])
    mde = res[(res["GROUP_BY"] == "holdout_pooled") & (res["GROUP_KEY"] == "min_detectable_lift")]
    assert len(mde) == 3 and (mde["VALUE"] > 0).all()
    slots = res[(res.GROUP_BY == "slot") & (res.METRIC == "buy_rate")]
    assert sorted(slots["GROUP_KEY"]) == ["1", "2", "3", "4", "5", "6"]
    assert ((slots["VALUE"] >= 0) & (slots["VALUE"] <= 1)).all()
    assert ((slots["CI_LOW"] <= slots["VALUE"]) & (slots["VALUE"] <= slots["CI_HIGH"])).all()
    # every 'buy' event really is a purchase of the picked product, after the send, within 90 days
    bad = snow.df("""SELECT COUNT(*) N FROM NBO_EMAIL_EVENTS b
                     WHERE b.RUN_DATE = %s AND b.EVENT_TYPE = 'buy' AND NOT EXISTS (
                       SELECT 1 FROM NBO_REBUILD.HM_ODS.HM_NBO_DWH_F_TRANSACTION t
                       JOIN NBO_REBUILD.HM_ODS.HM_NBO_DWH_D_ARTICLE a ON a.ARTICLE_ID = t.ARTICLE_ID
                       WHERE t.CUSTOMER_ID = b.CUSTOMER_ID AND a.PRODUCT_CODE = b.PRODUCT_CODE
                         AND t.T_DAT > b.RUN_DATE::DATE AND t.T_DAT <= DATEADD('day', 90, b.RUN_DATE::DATE))""",
                  (d,))
    assert bad["N"].iloc[0] == 0
    # re-running replaces, never doubles
    def counts():
        return [snow.df(f"SELECT COUNT(*) N FROM {t} WHERE RUN_DATE = %s", (d,))["N"].iloc[0]
                for t in ("NBO_EVAL_RESULTS", "NBO_EVAL_CUSTOMERS")]
    n1 = counts()
    assert n1[1] == e["emails"] + e["holdout_customers"]                    # one outcome row per customer of the send
    assert snow.call("NBO_EVALUATE", d, 90)["status"] == "SUCCESS"
    assert counts() == n1


def test_evaluation_waits_for_90_days(snow, chain):
    r = snow.call("NBO_EVALUATE", "2020-09-01", 90)       # purchase data ends before 2020-09-01 + 90 days
    assert r["status"] == "PENDING"
