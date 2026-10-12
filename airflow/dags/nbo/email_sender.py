"""
Sends the READY emails of one send date over SMTP and records each result in NBO_EMAIL_OUTBOX.

Each email is CLAIMED before it is sent (READY/FAILED -> SENDING, only if nobody else claimed it), then marked
SENT or FAILED. So:
  - a retry or a second worker never sends the same email twice
  - if Airflow dies between "sent" and "marked SENT", the row stays SENDING and is NOT sent again
    (at most once: for marketing email a missing email is better than a duplicate); the dashboard shows
    rows stuck in SENDING so a person can check
  - FAILED rows are tried again by the task's retries
"""
import logging

import pandas as pd

from nbo import snowflake as sf
from nbo.config import EMAIL_FROM, SMTP_CONN, required

log = logging.getLogger(__name__)


def pending(run_date):
    return sf.query("""SELECT EMAIL_ID, RECIPIENT_EMAIL, SUBJECT, HTML FROM NBO_EMAIL_OUTBOX
                       WHERE RUN_DATE = %s AND STATUS IN ('READY', 'FAILED') ORDER BY EMAIL_ID""", (run_date,))


def claim(run_date, email_id, recipient):
    return sf.execute("""UPDATE NBO_EMAIL_OUTBOX SET STATUS = 'SENDING', ERROR_MESSAGE = ''
                         WHERE RUN_DATE = %s AND EMAIL_ID = %s AND RECIPIENT_EMAIL = %s
                           AND STATUS IN ('READY', 'FAILED')""", (run_date, email_id, recipient)) == 1


def mark(run_date, email_id, recipient, status, error=""):
    sf.execute("""UPDATE NBO_EMAIL_OUTBOX SET STATUS = %s, SENT_AT = %s, ERROR_MESSAGE = %s
                  WHERE RUN_DATE = %s AND EMAIL_ID = %s AND RECIPIENT_EMAIL = %s AND STATUS = 'SENDING'""",
               (status, str(pd.Timestamp.utcnow()) if status == "SENT" else "", error[:1000],
                run_date, email_id, recipient))


def send_all(run_date):
    """Returns counts; raises if any email failed (so the task retries them and, in the end, alerts)."""
    sender = required("NBO_EMAIL_FROM", EMAIL_FROM)
    rows = pending(run_date)
    sent, failed, skipped = 0, [], 0
    if rows.empty:
        log.info("no READY emails for %s", run_date)
        return dict(sent=0, failed=0, skipped=0)
    from airflow.providers.smtp.hooks.smtp import SmtpHook
    with SmtpHook(smtp_conn_id=SMTP_CONN) as smtp:
        for r in rows.itertuples():
            if not claim(run_date, r.EMAIL_ID, r.RECIPIENT_EMAIL):
                skipped += 1                                    # someone else has it
                continue
            try:
                smtp.send_email_smtp(to=r.RECIPIENT_EMAIL, subject=r.SUBJECT, html_content=r.HTML,
                                     from_email=sender, mime_subtype="alternative",
                                     custom_headers={"X-NBO-Email-Id": r.EMAIL_ID})
                mark(run_date, r.EMAIL_ID, r.RECIPIENT_EMAIL, "SENT")
                sent += 1
            except Exception as e:
                log.exception("send failed: %s", r.EMAIL_ID)
                mark(run_date, r.EMAIL_ID, r.RECIPIENT_EMAIL, "FAILED", f"{type(e).__name__}: {e}")
                failed.append(r.EMAIL_ID)
    log.info("sent %s, failed %s, skipped %s", sent, len(failed), skipped)
    if failed:
        raise RuntimeError(f"{len(failed)} emails failed for {run_date}: {failed[:5]}")
    return dict(sent=sent, failed=0, skipped=skipped)
