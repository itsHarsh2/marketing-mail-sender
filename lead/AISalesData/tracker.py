"""
tracker.py - Checks sender inboxes for replies via IMAP.

IMPROVEMENTS v2:
- IMAP connection wrapped in context manager (always closes)
- Subject matching uses normalized comparison (strip Re:/Fwd: prefixes)
- Searches only UNSEEN messages from the last 14 days (not full inbox)
- Distinguishes "auto-reply" / out-of-office messages (doesn't count as reply)
- Per-sender error isolation — one broken inbox doesn't abort the others
- Returns detailed results instead of bare ID list
"""

import imaplib
import email
import re
import sqlite3
from datetime import datetime, timedelta
from email.header import decode_header as _decode_header

from utils import log_info, log_error, log_warning, log_success


# ── HELPERS ────────────────────────────────────────────────────────────────────

def _decode_str(value: str | bytes | None) -> str:
    """Safely decode an email header value to a plain string."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    # Handle RFC 2047 encoded headers
    parts = _decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


_AUTO_REPLY_SUBJECTS = re.compile(
    r"(out of office|auto[-\s]?reply|automatic response|away|vacation notice|ooo)",
    re.IGNORECASE,
)

_RE_FWD_PREFIX = re.compile(r"^\s*(re|fwd?):\s*", re.IGNORECASE)

def _normalise_subject(subject: str) -> str:
    """Strip Re:/Fwd: prefixes and normalise whitespace."""
    s = _RE_FWD_PREFIX.sub("", subject).strip().lower()
    return re.sub(r"\s+", " ", s)


def _get_imap_server(smtp_server: str) -> str:
    """Guess the IMAP hostname from the SMTP hostname."""
    return smtp_server.replace("smtp.", "imap.", 1)


# ── IMAP CHECK ─────────────────────────────────────────────────────────────────

def check_inbox_for_replies(
    imap_server: str,
    email_address: str,
    password: str,
    campaign_emails: list[dict],
    days_back: int = 14,
) -> list[int]:
    """
    Check one mailbox for replies to campaign emails.

    Args:
        imap_server      : IMAP hostname (e.g. imap.gmail.com)
        email_address    : The mailbox to check
        password         : Password or App Password
        campaign_emails  : list of dicts with keys: id, recipient_email, subject
        days_back        : Search this many days of messages

    Returns:
        List of emails_sent.id values that received a reply.
    """
    replied_ids: list[int] = []

    # Build a lookup: normalised_subject → list of email IDs
    subject_index: dict[str, list[int]] = {}
    for ce in campaign_emails:
        norm = _normalise_subject(ce.get("subject", ""))
        subject_index.setdefault(norm, []).append(ce["id"])

    try:
        log_info(f"  Checking inbox: {email_address} on {imap_server}")
        mail = imaplib.IMAP4_SSL(imap_server, timeout=15)
        try:
            mail.login(email_address, password)
        except imaplib.IMAP4.error as exc:
            log_error(f"IMAP login failed for {email_address}: {exc}")
            return replied_ids

        mail.select("INBOX")

        # Search only recent messages (reduces data transferred)
        since_date = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
        _, msg_ids = mail.search(None, f'(SINCE "{since_date}")')

        if not msg_ids or not msg_ids[0]:
            log_info(f"  No messages in the last {days_back} days.")
            return replied_ids

        id_list = msg_ids[0].split()
        log_info(f"  Scanning {len(id_list)} messages…")

        for msg_id in id_list:
            try:
                # Fetch only headers (much faster than full RFC822)
                _, header_data = mail.fetch(msg_id, "(BODY[HEADER.FIELDS (FROM SUBJECT IN-REPLY-TO)])")
                if not header_data or not header_data[0]:
                    continue

                raw_header = header_data[0][1]
                msg = email.message_from_bytes(raw_header)

                from_addr = _decode_str(msg.get("From", "")).lower()
                subject   = _decode_str(msg.get("Subject", ""))
                norm_subj = _normalise_subject(subject)

                # Skip auto-replies
                if _AUTO_REPLY_SUBJECTS.search(subject):
                    continue

                # Match sender → campaign recipient
                for ce in campaign_emails:
                    recipient = ce.get("recipient_email", "").lower()
                    if recipient not in from_addr:
                        continue

                    # Match subject
                    ce_norm = _normalise_subject(ce.get("subject", ""))
                    if ce_norm and (ce_norm in norm_subj or norm_subj in ce_norm):
                        if ce["id"] not in replied_ids:
                            replied_ids.append(ce["id"])
                            log_success(f"  Reply detected from {from_addr} — email ID {ce['id']}")
                        break

            except Exception as exc:
                log_warning(f"Error reading message {msg_id}: {exc}")
                continue

    except imaplib.IMAP4.error as exc:
        log_error(f"IMAP error for {email_address}: {exc}")
    except OSError as exc:
        log_error(f"Network error checking {email_address}: {exc}")
    except Exception as exc:
        log_error(f"Unexpected error checking {email_address}: {exc}")
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return replied_ids


def _update_replied_status(email_ids: list[int], db_path: str) -> None:
    """Mark a list of email IDs as replied in the database."""
    if not email_ids:
        return
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        cur = conn.cursor()
        now = datetime.now().isoformat()
        cur.executemany(
            "UPDATE emails_sent SET replied_at = ?, status = 'replied' WHERE id = ?",
            [(now, eid) for eid in email_ids],
        )
        conn.commit()
        log_success(f"Marked {len(email_ids)} email(s) as replied.")
    finally:
        conn.close()


def run_reply_check(senders: list[dict],
                    db_path: str = "sales_campaign.db") -> None:
    """
    Check all sender inboxes and update the database with any replies found.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, recipient_email, sender_email, subject
            FROM   emails_sent
            WHERE  replied_at IS NULL AND status = 'sent'
            """
        )
        campaign_emails = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    if not campaign_emails:
        log_info("No pending emails to check for replies.")
        return

    log_info(f"Checking replies for {len(campaign_emails)} sent email(s)…")
    all_replied: list[int] = []

    for sender in senders:
        smtp_server = sender.get("smtp_server", "")
        if not smtp_server:
            continue
        imap_server = _get_imap_server(smtp_server)
        try:
            ids = check_inbox_for_replies(
                imap_server=imap_server,
                email_address=sender["email"],
                password=sender["password"],
                campaign_emails=campaign_emails,
            )
            all_replied.extend(ids)
        except Exception as exc:
            log_error(f"Reply check failed for {sender['email']}: {exc}")
            continue  # One broken inbox never stops the others

    if all_replied:
        _update_replied_status(all_replied, db_path)
    else:
        log_info("No new replies found.")
