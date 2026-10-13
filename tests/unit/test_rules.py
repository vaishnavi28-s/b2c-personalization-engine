"""
Unit tests of single rules: pure Python, no database, run on every pull request in seconds.
"""
from collections import namedtuple

import pandas as pd

import evaluation as ev
import inference as inf
import model_training as mt


# ---------------- model gate ----------------
def test_gate_first_model_passes():
    assert mt.gate({"taste_p5": 0.05, "basket_p5": 0.04})[0] == "PASS"


def test_gate_fails_when_more_than_20_percent_worse_than_last_month():
    status, reason = mt.gate({"taste_p5": 0.03, "basket_p5": 0.04}, {"taste_p5": 0.05, "basket_p5": 0.04})
    assert status == "FAIL" and "taste" in reason


def test_gate_passes_small_drop():
    assert mt.gate({"taste_p5": 0.045, "basket_p5": 0.04}, {"taste_p5": 0.05, "basket_p5": 0.04})[0] == "PASS"


def test_gate_fails_without_measurement():
    assert mt.gate({"taste_p5": None, "basket_p5": 0.04})[0] == "FAIL"


# ---------------- best-seller baseline ----------------
def _matrices():
    """3 customers x 6 items. Training history and the buys of the 15 test days."""
    from scipy.sparse import coo_matrix
    train = coo_matrix(([1, 1, 1], ([0, 1, 2], [0, 0, 1])), shape=(3, 6))       # c0 has i0, c1 has i0, c2 has i1
    test = coo_matrix(([1, 1, 1], ([0, 1, 1], [1, 2, 5])), shape=(3, 6))         # c2 bought nothing
    return train, test


def test_baseline_skips_items_the_customer_already_has_and_scores_only_buyers():
    train, test = _matrices()
    index = {f"a{i}": i for i in range(6)}
    # best sellers a0 > a1 > a2 > a3 > a4 > a5, k=2: c0 (has a0) gets a1, a2 -> 1 hit; c1 (has a0) gets a1, a2 -> 1 hit
    assert mt.baseline_precision(index, ["a0", "a1", "a2", "a3", "a4", "a5"], test, train, k=2) == 0.5


def test_baseline_ignores_items_the_model_does_not_know():
    train, test = _matrices()
    index = {f"a{i}": i for i in range(6)}
    assert mt.baseline_precision(index, ["zz", "a5", "a2"], test, train, k=2) == 0.5       # c0: 0 hits, c1: 2 hits


def test_baseline_without_buyers_is_none():
    from scipy.sparse import coo_matrix
    train, _ = _matrices()
    assert mt.baseline_precision({"a0": 0}, ["a0"], coo_matrix((3, 6)), train) is None


def test_beats_baseline():
    assert mt.beats_baseline({"taste_p5": 0.02, "taste_base_p5": 0.01, "basket_p5": 0.03, "basket_base_p5": 0.01})[0]
    ok, why = mt.beats_baseline({"taste_p5": 0.01, "taste_base_p5": 0.012, "basket_p5": 0.03, "basket_base_p5": 0.01})
    assert not ok and "taste" in why and "basket" not in why
    assert mt.beats_baseline({"taste_p5": 0.01, "taste_base_p5": None, "basket_p5": None, "basket_base_p5": 0.01})[0]


def test_losing_to_the_baseline_is_not_a_gate_failure():
    """The gate blocks regressions only; losing to best sellers is a WARN for a person (no better model exists)."""
    assert mt.gate({"taste_p5": 0.005, "basket_p5": 0.04, "taste_base_p5": 0.02, "basket_base_p5": 0.01})[0] == "PASS"


# ---------------- season rules / sections ----------------
def test_season_rules_block_coats_in_june_and_swimwear_in_december():
    df = pd.DataFrame({"PGROUP": ["Garment Upper body", "Swimwear", "Garment Full body"],
                       "PTYPE": ["Coat", "Bikini top", "Dress"]})
    assert list(mt.season_blocked(df, 6, mt.DEFAULT_SEASON_RULES)) == [True, False, False]
    assert list(mt.season_blocked(df, 12, mt.DEFAULT_SEASON_RULES)) == [False, True, False]


def test_sections():
    got = mt.section_of(["Ladieswear", "Divided", "Menswear", "Baby/Children", "Sport", "Sport"],
                        ["x", "x", "x", "x", "Men Sport", "Kids Girl"])
    assert list(got) == ["Women", "Young", "Men", "Kids", "Men", "Kids"]


# ---------------- her profile ----------------
def test_baby_only_buyer_gets_women_section_for_slot_4():
    baby = pd.DataFrame({"ARTICLE_ID": ["a", "b"], "PTYPE": ["Bodysuit", "Trousers"], "COLOUR": ["White", "Blue"],
                         "PATTERN": ["Solid", "Solid"], "SEC": ["Kids", "Kids"]})
    p = inf.her_profile(baby)
    assert p["section"] == "Women" and p["section_default"] is True and p["n_items"] == 2


def test_adult_buyer_gets_her_most_bought_section():
    h = pd.DataFrame({"ARTICLE_ID": list("abc"), "PTYPE": ["Top"] * 3, "COLOUR": ["Red"] * 3,
                      "PATTERN": ["Solid"] * 3, "SEC": ["Men", "Men", "Kids"]})
    p = inf.her_profile(h)
    assert p["section"] == "Men" and p["section_default"] is False


# ---------------- slots 5/6 novelty (AND -> OR) ----------------
def test_novelty_and_needs_a_new_type_colour_pair_or_needs_type_or_colour_new():
    prof = dict(pairs={("Dress", "Black")}, types={"Dress"}, colours={"Black"})
    c = pd.DataFrame({"PTYPE": ["Dress", "Dress", "Top", "Top"], "COLOUR": ["Black", "Red", "Black", "Red"]})
    assert len(inf.novelty(c, prof, "AND")) == 3            # only (Dress, Black) is not new
    assert len(inf.novelty(c, prof, "OR")) == 3             # Dress/Red, Top/Black, Top/Red have something new
    empty = inf.novelty(c.iloc[:0], prof, "AND")            # the empty-list bug: must not drop columns
    assert list(empty.columns) == ["PTYPE", "COLOUR"]


# ---------------- email rules ----------------
Card = namedtuple("Card", "ARTICLE_ID PRODUCT_CODE PGROUP PROD_NAME DEPT SEC PTYPE COLOUR PATTERN")


def card(a, p, g="Garment Upper body"):
    return Card(a, p, g, "n", "d", "Women", "Top", "Red", "Solid")


def test_email_rules():
    e = inf.Email(dict(bought={"A1"}), shown={"P9": 10}, cap=10)
    assert not e.ok(card("A1", "P1"))                       # she bought it
    assert not e.ok(card("A2", "P9"))                       # product already in 20% of emails
    e.take(1, card("A3", "P3", "Swimwear"), "model", "x", 1.0)
    assert not e.ok(card("A4", "P3"))                       # same product twice in one email
    assert not e.ok(card("A5", "P5", "Swimwear"))           # second swimwear
    assert e.ok(card("A6", "P6"))


def test_display_order_is_a_shuffle_and_stable():
    a = inf.display_order("E20191201_c1")
    assert sorted(a) == [1, 2, 3, 4, 5, 6] and sorted(a.values()) == [1, 2, 3, 4, 5, 6]
    assert a == inf.display_order("E20191201_c1")


def test_holdout_is_about_5_percent_and_stable():
    ids = [f"c{i}" for i in range(20000)]
    share = sum(inf.is_holdout(c) for c in ids) / len(ids)
    assert 0.04 < share < 0.06
    assert inf.is_holdout("c123") == inf.is_holdout("c123")


def test_hard_checks():
    assert inf.hard_check({"emails_complete_share": 1.0, "cap_broken": 0, "swim_max_per_email": 1,
                           "customers_failed": 0}) == []
    assert len(inf.hard_check({"emails_complete_share": 0.9, "cap_broken": 1})) == 2


# ---------------- evaluation (buys from the purchase data) ----------------
def test_scores_per_slot_position_and_email():
    c = pd.DataFrame({"EMAIL_ID": ["E1"] * 6 + ["E2"] * 6, "CUSTOMER_ID": ["c1"] * 6 + ["c2"] * 6,
                      "GRP": ["EMAIL"] * 12, "SLOT": list(range(1, 7)) * 2, "POSITION": [3, 1, 2, 6, 5, 4] * 2,
                      "BUY": [1, 0, 0, 0, 0, 0] + [1, 1, 0, 0, 0, 0], "BUY_ITEM": [1] + [0] * 11})
    rows = {(r["GROUP_BY"], r["GROUP_KEY"], r["METRIC"]): r for r in ev.scores(c)}
    assert rows[("slot", "1", "buy_rate")]["VALUE"] == 1.0 and rows[("slot", "2", "buy_rate")]["VALUE"] == 0.5
    assert rows[("position", "3", "buy_rate")]["VALUE"] == 1.0              # slot 1 was shown in position 3
    assert rows[("slot", "1", "buy_item_rate")]["VALUE"] == 0.5
    assert rows[("email", "all", "email_buy_rate")]["VALUE"] == 1.0
    assert rows[("email", "all", "buys_per_email")]["VALUE"] == 1.5
    assert not any(k[2].startswith(("click", "open", "engagement")) for k in rows)


def test_holdout_customers_are_not_scored_as_emails():
    c = pd.DataFrame({"EMAIL_ID": ["E1", "H1"], "CUSTOMER_ID": ["c1", "c2"], "GRP": ["EMAIL", "HOLDOUT"],
                      "SLOT": [1, 1], "POSITION": [1, 1], "BUY": [0.0, 1.0], "BUY_ITEM": [0.0, 1.0]})
    rows = {(r["GROUP_BY"], r["GROUP_KEY"], r["METRIC"]): r["VALUE"] for r in ev.scores(c)}
    assert rows[("slot", "1", "buy_rate")] == 0.0


def test_ci_mean_contains_the_mean():
    v, lo, hi = ev.ci_mean([0, 1] * 500)
    assert lo <= v <= hi and abs(v - 0.5) < 1e-9


def test_ci_diff_of_identical_groups_includes_zero():
    v, lo, hi = ev.ci_diff([0, 1] * 300, [1, 0] * 300)
    assert v == 0 and lo <= 0 <= hi


def _outcomes(n_email, n_hold, sends=1, email_rate=0.5, hold_rate=0.5):
    rows = []
    for s_ in range(sends):
        for grp, n, rate in (("EMAIL", n_email, email_rate), ("HOLDOUT", n_hold, hold_rate)):
            for i in range(n):
                buy = float(i < rate * n)
                rows.append(dict(RUN_DATE=f"2020-0{s_ + 1}-01", CUSTOMER_ID=f"{grp}{i}", GRP=grp, ANY_BUY=buy,
                                 SPEND=buy * 0.05, PICK_BUY=0.0))
    return pd.DataFrame(rows)


def test_holdout_rows_have_lift_level_and_min_detectable_lift():
    rows = {(r["GROUP_KEY"], r["METRIC"]): r for r in ev.holdout(_outcomes(400, 100, email_rate=0.6, hold_rate=0.5))}
    lift = rows[("email_minus_holdout", "any_buy_rate")]
    assert abs(lift["VALUE"] - 0.1) < 1e-9 and lift["CI_LOW"] <= 0.1 <= lift["CI_HIGH"] and lift["N"] == 500
    assert rows[("holdout_level", "any_buy_rate")]["VALUE"] == 0.5
    assert 0 < rows[("min_detectable_lift", "any_buy_rate")]["VALUE"] < 1
    assert {m for _, m in rows} == {"any_buy_rate", "spend_per_customer", "picked_item_buy_rate"}


def test_pooling_counts_customers_not_sends():
    """The same customers in 4 sends are still 500 customers: the pooled range must NOT shrink like 2000 would."""
    one = {(r["GROUP_KEY"], r["METRIC"]): r for r in ev.pooled(_outcomes(400, 100, sends=1))}
    four = {(r["GROUP_KEY"], r["METRIC"]): r for r in ev.pooled(_outcomes(400, 100, sends=4))}
    assert four[("email_minus_holdout", "any_buy_rate")]["N"] == 500
    k = ("min_detectable_lift", "any_buy_rate")
    assert abs(four[k]["VALUE"] - one[k]["VALUE"]) < 1e-9
    assert all(r["GROUP_BY"] == "holdout_pooled" for r in four.values())


def test_more_customers_detect_smaller_lifts():
    small = ev.min_detectable([0, 1] * 125, [0, 1] * 25)
    big = ev.min_detectable([0, 1] * 3000, [0, 1] * 600)
    assert big < small / 4


# ---------------- the email ----------------
def test_email_shows_6_cards_in_display_order_without_links_or_pixel():
    import emails
    cards = pd.DataFrame({"EMAIL_ID": ["E20200601_c1"] * 6, "ARTICLE_ID": [f"0{i}" for i in range(6)],
                          "PROD_NAME": [f"Item {i}" for i in range(6)], "TYPE": ["Top"] * 6, "COLOUR": ["Black"] * 6,
                          "SLOT": [f"Slot {i}" for i in range(6)], "POSITION": [6, 5, 4, 3, 2, 1]})
    html = emails.render(cards, {}, "Summer vacation", "Vaish")
    assert html.count("SLOT ") == 6 and "Hi Vaish," in html
    assert html.index("Item 5") < html.index("Item 0")                     # shown by POSITION, not by slot
    assert "<a " not in html and "href" not in html                         # nothing clickable
    assert 'width="1" height="1"' not in html                              # no tracking pixel


def test_unknown_colour_is_not_shown_to_the_customer():
    import emails
    r = namedtuple("Card", "PROD_NAME COLOUR TYPE SLOT")("Penelope", "Unknown", "Dress", "Your style")
    html = emails.card(r, None)
    assert "unknown" not in html.lower() and ">dress<" in html
    assert "black &middot; dress" in emails.card(r._replace(COLOUR="Black"), None)



def test_every_occasion_has_short_slot_labels():
    """Every occasion the email knows gets its own slot 6 label, and no slot label is too long for one line on a
    card (a wrapped label pushes that card's photo down and the row no longer lines up)."""
    import emails
    for occasion in emails.HEADLINES:
        assert occasion in inf.NEW_FOR, occasion
        assert "in season now" not in inf.NEW_FOR[occasion].lower()
    labels = [s for s in inf.SLOTS.values() if "{" not in s] + list(inf.NEW_FOR.values()) + [inf.NEW_FOR_DEFAULT]
    labels += [o for o in inf.NEW_FOR]                                   # slot 2 shows the occasion itself
    assert max(len(x) for x in labels) <= 28, max(labels, key=len)


def test_card_label_never_wraps():
    import emails
    Card = namedtuple("Card", "PROD_NAME COLOUR TYPE SLOT")
    html = emails.card(Card("Shirt", "blue", "shirt", "New for the festive weeks"), "")
    assert "white-space:nowrap" in html and "NEW FOR THE FESTIVE WEEKS" in html
