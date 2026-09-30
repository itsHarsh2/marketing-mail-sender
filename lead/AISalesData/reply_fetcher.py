"""
reply_fetcher.py — pull prospect replies out of the sender mailboxes.

Outgoing mail carries no Reply-To header, so a reply goes to the From
address — that is, to whichever account in the rotation pool sent that
particular email. Replies therefore scatter across every sender mailbox,
and this module is what gathers them back into one place.

MATCHING
    1. In-Reply-To / References containing a Message-ID we stored — exact.
    2. Otherwise the sender address matches someone we emailed AND the
       normalised subject overlaps. Needed for the 300+ emails sent before
       Message-IDs were recorded.

Auto-replies and out-of-office notices are skipped: treating them as real
replies would wrongly suppress follow-ups.
"""

import email
import imaplib
import re
from datetime import datetime, timedelta
from email.header import decode_header as _decode_header
from email.utils import parsedate_to_datetime, parseaddr

from database import (
    get_sent_index, insert_reply, mark_email_replied, mark_recipient_replied,
)
from utils import log_info, log_warning, log_error

IMAP_TIMEOUT = 20
MAX_BODY_CHARS = 20000

_AUTO_REPLY_RE = re.compile(
    r"(out of office|auto[-\s]?reply|automatic reply|automatic response|"
    r"away from|vacation|undeliverable|delivery status|mail delivery|"
    r"returned mail|failure notice)",
    re.IGNORECASE,
)
_RE_FWD_PREFIX = re.compile(r"^\s*((re|fwd?|aw|sv)\s*:\s*)+", re.IGNORECASE)


def _decode(value) -> str:
    """Decode an RFC 2047 header to plain text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    out = []
    for part, charset in _decode_header(value):
        if isinstance(part, bytes):
            out.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(part)
    return " ".join(out).strip()


def _normalise_subject(subject: str) -> str:
    s = _RE_FWD_PREFIX.sub("", subject or "").strip().lower()
    return re.sub(r"\s+", " ", s)


def imap_host_for(sender: dict) -> str:
    """
    IMAP host for a sender account.

    Uses an explicit imap_server when set; otherwise swaps the smtp. prefix,
    which covers Gmail and Hostinger but leaves mail.yourdomain.com alone
    (usually correct for cPanel).
    """
    explicit = (sender.get("imap_server") or "").strip()
    if explicit:
        return explicit
    host = (sender.get("smtp_server") or "").strip()
    return host.replace("smtp.", "imap.", 1) if host.startswith("smtp.") else host


def _extract_body(msg) -> str:
    """Best-effort plain-text body."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
               "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8",
                                          errors="replace")[:MAX_BODY_CHARS]
                except Exception:
                    continue
        # fall back to HTML, stripped
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    payload = (part.get_payload(decode=True) or b"").decode(
                        part.get_content_charset() or "utf-8", errors="replace")
                    return re.sub(r"<[^>]+>", " ", payload)[:MAX_BODY_CHARS]
                except Exception:
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8",
                              errors="replace")[:MAX_BODY_CHARS]
    except Exception:
        return str(msg.get_payload())[:MAX_BODY_CHARS]


def _build_indexes(sent_rows):
    """by Message-ID, and by recipient address -> rows we sent them."""
    by_msgid, by_recipient = {}, {}
    for row in sent_rows:
        mid = (row.get("message_id") or "").strip()
        if mid:
            by_msgid[mid] = row
        addr = (row.get("recipient_email") or "").strip().lower()
        if addr:
            by_recipient.setdefault(addr, []).append(row)
    return by_msgid, by_recipient


def _match(msg, from_addr, subject, by_msgid, by_recipient):
    """Find the email we sent that this message is replying to."""
    refs = " ".join(filter(None, [msg.get("In-Reply-To", ""),
                                  msg.get("References", "")]))
    for mid, row in by_msgid.items():
        if mid and mid in refs:
            return row

    candidates = by_recipient.get(from_addr, [])
    if not candidates:
        return None
    norm = _normalise_subject(subject)
    for row in candidates:
        theirs = _normalise_subject(row.get("subject", ""))
        if theirs and (theirs in norm or norm in theirs):
            return row
    # Same person, subject changed — still their reply; use the latest thread.
    return candidates[0]


def fetch_replies_for_mailbox(sender: dict, days_back: int = 30,
                              progress_cb=None) -> dict:
    """
    Scan one sender mailbox for replies. Returns a summary dict.

    Only messages from addresses we actually emailed are stored, so ordinary
    mail in that inbox is ignored.
    """
    address = (sender.get("email") or "").strip()
    password = sender.get("password") or ""
    host = imap_host_for(sender)
    summary = {"mailbox": address, "scanned": 0, "new": 0,
               "skipped_auto": 0, "error": None}

    if not host or not address or not password:
        summary["error"] = "Missing IMAP host, address, or password."
        return summary

    sent_rows = get_sent_index()
    by_msgid, by_recipient = _build_indexes(sent_rows)

    try:
        mail = imaplib.IMAP4_SSL(host, timeout=IMAP_TIMEOUT)
    except Exception as exc:
        summary["error"] = f"Cannot reach {host}: {exc}"
        return summary

    try:
        try:
            mail.login(address, password)
        except imaplib.IMAP4.error as exc:
            summary["error"] = (
                f"IMAP login failed for {address}: {exc}. "
                "Gmail needs IMAP enabled and an app password."
            )
            return summary

        mail.select("INBOX")
        since = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
        typ, data = mail.search(None, f'(SINCE "{since}")')
        ids = data[0].split() if data and data[0] else []
        summary["scanned"] = len(ids)
        log_info(f"  {address}: scanning {len(ids)} message(s) since {since}")

        for n, uid in enumerate(ids, 1):
            if progress_cb:
                progress_cb(address, n, len(ids))
            try:
                typ, raw = mail.fetch(uid, "(RFC822)")
                if not raw or not raw[0]:
                    continue
                msg = email.message_from_bytes(raw[0][1])

                subject   = _decode(msg.get("Subject", ""))
                from_name, from_addr = parseaddr(_decode(msg.get("From", "")))
                from_addr = (from_addr or "").lower()

                if not from_addr or from_addr == address.lower():
                    continue
                if _AUTO_REPLY_RE.search(subject):
                    summary["skipped_auto"] += 1
                    continue

                row = _match(msg, from_addr, subject, by_msgid, by_recipient)
                if not row:
                    continue      # not a reply to our outreach

                try:
                    received = parsedate_to_datetime(msg.get("Date", ""))
                    received_s = received.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    received_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                new_id = insert_reply(
                    email_sent_id=row["id"], lead_id=row.get("lead_id"),
                    mailbox=address, from_email=from_addr, from_name=from_name,
                    subject=subject, body=_extract_body(msg),
                    received_at=received_s,
                    message_id=(msg.get("Message-ID") or "").strip(),
                    imap_uid=uid.decode() if isinstance(uid, bytes) else str(uid),
                )
                if new_id:
                    summary["new"] += 1
                    mark_email_replied(row["id"])
                    # Stop every pending follow-up to this person, not just
                    # the one thread they answered.
                    mark_recipient_replied(from_addr)
                    log_info(f"  ✉️  Reply from {from_addr} → {address}")

            except Exception as exc:
                log_warning(f"  Could not read message {uid}: {exc}")
                continue

    except Exception as exc:
        summary["error"] = str(exc)
        log_error(f"IMAP error for {address}: {exc}")
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return summary


def fetch_all_replies(senders: list[dict], days_back: int = 30,
                      progress_cb=None) -> list[dict]:
    """Scan every configured mailbox. One bad inbox never stops the others."""
    results = []
    usable = [s for s in senders if s.get("email") and s.get("password")]
    log_info(f"📥 Checking {len(usable)} mailbox(es) for replies…")
    for s in usable:
        try:
            results.append(fetch_replies_for_mailbox(s, days_back, progress_cb))
        except Exception as exc:
            results.append({"mailbox": s.get("email", "?"), "scanned": 0,
                            "new": 0, "skipped_auto": 0, "error": str(exc)})
    total_new = sum(r["new"] for r in results)
    log_info(f"📥 Reply check complete — {total_new} new reply(ies).")
    return results
