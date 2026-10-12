from __future__ import annotations

import html

# ======================================================================
# emails.py - turns a send's picks into emails
#   READY    full HTML for the test recipients (NBO_EMAIL_RECIPIENTS) - Airflow really sends these
#   DRY_RUN  one row per emailed customer, no HTML - recorded as "would have been sent"
# The system ends at the sent email: cards show photo, name, colour and type, with no links and no tracking
# pixel. In a real company each card would link to the shop's product page through the website's click
# tracking (and the email would carry an open pixel) - see the README.
# ======================================================================
import re

import pandas as pd

import model_training as mt

IMAGES_STAGE = "NBO_REBUILD.HM_ML.NBO_IMAGES"     # product photos (one stage, shared by DEV and PROD)
LINK_SECONDS = 604800                             # photo links valid 7 days
UNKNOWN = {"unknown", "undefined", "none", "nan", "other"}     # data placeholders, never shown to a customer
SHORT = {"HW": "High-waist", "RW": "Regular-waist", "TRS": "trousers", "L/S": "long-sleeve", "S/S": "short-sleeve"}
HEADLINES = {
    "active / sport":           ("Ready, set, move",       "six picks for your active season"),
    "christmas / party season": ("Dressed for the season", "six picks for the festive weeks"),
    "summer vacation":          ("Holiday mode: on",       "six picks for sunny days"),
    "halloween":                ("Something to dress up",  "six picks for the season"),
    "valentine's day":          ("Picked with love",       "six picks for the days ahead"),
    "easter":                   ("Fresh for spring",       "six picks for the new season"),
    "mother's day":             ("Little treats",          "six picks for the days ahead"),
    "in season now":            ("In season now",          "six picks for right now"),
    "popular right now":        ("Picked for you",         "six picks chosen from your style"),
}


def clean_name(name):
    """Shop-floor product names -> customer-facing names (no codes, no store labels, no shorthand)."""
    s = str(name or "")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"(?<!\w)(H&M|HM\+?)(?!\w)", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" .-")
    if s.isupper():
        s = s.title()
    s = " ".join(SHORT.get(w.upper(), w) for w in s.split(" "))
    return s or "New in"


def card(r, img):
    name = html.escape(clean_name(r.PROD_NAME))
    parts = [str(x).strip().lower() for x in (r.COLOUR, r.TYPE)]
    sub = " &middot; ".join(html.escape(x) for x in parts if x and x not in UNKNOWN)
    label = html.escape(str(r.SLOT).upper())
    pic = (f'<img src="{html.escape(img)}" width="260" alt="{name}" style="display:block;width:100%;max-width:260px;'
           f'height:auto;border:0;background:#ece8e2;">' if img else
           '<div style="width:100%;height:320px;background:#ece8e2;"></div>')
    return ('<td width="50%" valign="top" style="padding:10px;">'
            f'<div style="font:600 11px Arial,Helvetica,sans-serif;letter-spacing:1.5px;color:#8a7f72;padding:0 0 6px;">{label}</div>'
            f'{pic}'
            f'<div style="font:600 15px Georgia,serif;color:#1f1d1a;padding:10px 0 2px;">{name}</div>'
            f'<div style="font:13px Arial,Helvetica,sans-serif;color:#6b645b;padding:0 0 10px;">{sub}</div></td>')


def render(cards, imgs, occasion, name):
    """One email. Cards are shown in their shuffled POSITION order, 2 per row."""
    title, intro = HEADLINES.get(str(occasion).lower(), (str(occasion), "six picks chosen for you"))
    hello = f"Hi {html.escape(name)}," if name else "Hi,"
    cells = [card(r, imgs.get(r.ARTICLE_ID)) for r in cards.sort_values("POSITION").itertuples()]
    rows = "".join(f"<tr>{''.join(cells[i:i + 2])}</tr>" for i in range(0, len(cells), 2))
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            '<body style="margin:0;padding:0;background:#f6f4f1;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f4f1;"><tr><td align="center">'
            '<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:100%;background:#ffffff;">'
            '<tr><td style="padding:28px 30px 8px;font:600 12px Arial,Helvetica,sans-serif;letter-spacing:3px;color:#8a7f72;">YOUR 6 PICKS</td></tr>'
            f'<tr><td style="padding:0 30px;font:400 32px Georgia,serif;color:#1f1d1a;">{html.escape(title)}</td></tr>'
            f'<tr><td style="padding:10px 30px 18px;font:15px Arial,Helvetica,sans-serif;color:#4a443d;">{hello} {html.escape(intro)}.</td></tr>'
            f'<tr><td style="padding:0 20px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>'
            '<tr><td style="padding:18px 30px 28px;border-top:1px solid #ece8e2;font:11px Arial,Helvetica,sans-serif;color:#9a9188;line-height:1.6;">'
            'Portfolio demo: picks generated by a recommender running in Snowflake on a public fashion dataset. '
            'Not a real store.</td></tr>'
            '</table></td></tr></table></body></html>')


def image_links(articles):
    """Presigned photo links for the given articles (one query; only photos that exist on the stage)."""
    if not articles:
        return {}
    ids = ",".join(f"'{str(a).zfill(10)}.jpg'" for a in articles)
    r = mt.q(f"""SELECT RELATIVE_PATH, GET_PRESIGNED_URL(@{IMAGES_STAGE}, RELATIVE_PATH, {LINK_SECONDS}) AS U
                 FROM DIRECTORY(@{IMAGES_STAGE}) WHERE RELATIVE_PATH IN ({ids})""")
    by_file = dict(zip(r["RELATIVE_PATH"], r["U"]))
    return {a: by_file.get(f"{str(a).zfill(10)}.jpg") for a in articles}


def build_outbox(d, images=True):
    """READY emails for the active recipients (each shown one real customer's picks) + a DRY_RUN row
    for every emailed customer. Re-run safe: the date's outbox rows are replaced. Returns counts."""
    d = mt.ymd(d)
    items = mt.q(f"""SELECT EMAIL_ID, CUSTOMER_ID, POS, POSITION, SLOT, OCCASION, ARTICLE_ID, PROD_NAME, TYPE, COLOUR
                     FROM {mt.DB}.NBO_RECOMMENDATION_ITEMS WHERE RUN_DATE = '{d}' AND GRP = 'EMAIL'""")
    if items.empty:
        raise ValueError(f"No emailed customers stored for {d}")
    rec = mt.q(f"""SELECT RECIPIENT_ID, RECIPIENT_EMAIL, RECIPIENT_NAME, DEMO_CUSTOMER_ID
                   FROM {mt.DB}.NBO_EMAIL_RECIPIENTS WHERE ACTIVE ORDER BY RECIPIENT_ID""")
    demo = sorted(items["CUSTOMER_ID"].unique())
    now, out = str(pd.Timestamp.utcnow()), []
    for i, r in enumerate(rec.itertuples()):
        cid = r.DEMO_CUSTOMER_ID if isinstance(r.DEMO_CUSTOMER_ID, str) and r.DEMO_CUSTOMER_ID in set(demo) \
            else (demo[i] if i < len(demo) else None)
        if cid is None:
            continue
        mine = items[items["CUSTOMER_ID"] == cid]
        imgs = image_links(list(mine["ARTICLE_ID"])) if images else {}
        occasion = mine["OCCASION"].iloc[0]
        title = HEADLINES.get(str(occasion).lower(), (str(occasion), ""))[0]
        out.append(dict(RUN_DATE=d, EMAIL_ID=mine["EMAIL_ID"].iloc[0], RECIPIENT_EMAIL=r.RECIPIENT_EMAIL,
                        CUSTOMER_ID=cid, SUBJECT=f"Your 6 picks: {title}",
                        HTML=render(mine, imgs, occasion, r.RECIPIENT_NAME if isinstance(r.RECIPIENT_NAME, str) else ""),
                        STATUS="READY", CREATED_AT=now, SENT_AT="", ERROR_MESSAGE=""))
    emails = items.drop_duplicates("EMAIL_ID")
    for e in emails.itertuples():
        out.append(dict(RUN_DATE=d, EMAIL_ID=e.EMAIL_ID, RECIPIENT_EMAIL="", CUSTOMER_ID=e.CUSTOMER_ID,
                        SUBJECT=f"Your 6 picks: {e.OCCASION}", HTML="", STATUS="DRY_RUN", CREATED_AT=now,
                        SENT_AT="", ERROR_MESSAGE=""))
    mt.session.sql(f"DELETE FROM {mt.DB}.NBO_EMAIL_OUTBOX WHERE RUN_DATE = '{d}'").collect()
    db, schema = mt.DB.split(".")
    mt.session.write_pandas(pd.DataFrame(out), "NBO_EMAIL_OUTBOX", database=db, schema=schema,
                            auto_create_table=False)
    return dict(ready=sum(o["STATUS"] == "READY" for o in out), dry_run=len(emails))
