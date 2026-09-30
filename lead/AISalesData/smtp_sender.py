"""
smtp_sender.py - Sends emails through multiple SMTP accounts with rotation.

IMPROVEMENTS v2:
- test_sender() validates credentials BEFORE the campaign starts
- Per-sender error tracking: disables a sender after 3 consecutive failures
- SSL vs STARTTLS auto-detected from port (465 → SSL, otherwise STARTTLS)
- Unsubscribe / List-Unsubscribe header added (improves deliverability)
- Message-ID and Date headers set explicitly (required by RFC 5322)
- HTML entities in body properly escaped
- send_followup() correctly threads on the original sender (not rotation)
- All SMTP connections use a 20-second timeout
- Comprehensive per-sender stats (sent, failed, last_error)
"""

import smtplib
import ssl
import html
import hashlib
import time
import random
import itertools
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from typing import Iterator

from utils import (
    build_signature, log_info, log_error, log_warning, log_success,
    sanitize_credential, non_ascii_report,
)

# Shown when a credential still contains characters SMTP AUTH cannot encode.
_NON_ASCII_HELP = (
    "This usually happens when a password is copied from a web page — "
    "Gmail shows app passwords as 'abcd efgh ijkl mnop' and the spaces are "
    "often invisible non-breaking spaces. Retype the password by hand, or "
    "enter a Gmail app password as 16 characters with no spaces."
)

# ── CONSTANTS ──────────────────────────────────────────────────────────────────

MAX_EMAILS_PER_SENDER = 50   # per session
MAX_CONSECUTIVE_ERRORS = 3   # after this many in a row, the sender is disabled
SMTP_TIMEOUT = 20             # seconds


# ── PROVIDER PRESETS ───────────────────────────────────────────────────────────

PROVIDER_PRESETS: dict[str, dict] = {
    "Custom / Other":           {"smtp_server": "",                                 "smtp_port": 587, "use_ssl": False},
    "Gmail":                    {"smtp_server": "smtp.gmail.com",                   "smtp_port": 587, "use_ssl": False},
    "Hostinger":                {"smtp_server": "smtp.hostinger.com",               "smtp_port": 587, "use_ssl": False},
    "Hostinger (SSL)":          {"smtp_server": "smtp.hostinger.com",               "smtp_port": 465, "use_ssl": True},
    "Webmail / cPanel":         {"smtp_server": "mail.yourdomain.com",              "smtp_port": 587, "use_ssl": False},
    "Webmail / cPanel (SSL)":   {"smtp_server": "mail.yourdomain.com",              "smtp_port": 465, "use_ssl": True},
    "Outlook / Hotmail":        {"smtp_server": "smtp.live.com",                    "smtp_port": 587, "use_ssl": False},
    "Office 365":               {"smtp_server": "smtp.office365.com",               "smtp_port": 587, "use_ssl": False},
    "Yahoo Mail":               {"smtp_server": "smtp.mail.yahoo.com",              "smtp_port": 587, "use_ssl": False},
    "Zoho Mail":                {"smtp_server": "smtp.zoho.com",                    "smtp_port": 587, "use_ssl": False},
    "Zoho Mail (SSL)":          {"smtp_server": "smtp.zoho.com",                    "smtp_port": 465, "use_ssl": True},
    "SendGrid":                 {"smtp_server": "smtp.sendgrid.net",                "smtp_port": 587, "use_ssl": False},
    "Mailgun":                  {"smtp_server": "smtp.mailgun.org",                 "smtp_port": 587, "use_ssl": False},
    "Amazon SES":               {"smtp_server": "email-smtp.us-east-1.amazonaws.com","smtp_port": 587, "use_ssl": False},
    "iCloud Mail":              {"smtp_server": "smtp.mail.me.com",                 "smtp_port": 587, "use_ssl": False},
    "Titan Email":              {"smtp_server": "smtp.titan.email",                 "smtp_port": 587, "use_ssl": False},
    "Titan Email (SSL)":        {"smtp_server": "smtp.titan.email",                 "smtp_port": 465, "use_ssl": True},
    "ProtonMail (Bridge)":      {"smtp_server": "127.0.0.1",                        "smtp_port": 1025,"use_ssl": False},
}


# ── SENDER VALIDATION ──────────────────────────────────────────────────────────

def test_sender(sender: dict) -> dict:
    """
    Attempt to authenticate with the SMTP server to verify credentials.

    Returns:
        {"ok": True}  on success
        {"ok": False, "error": "human-readable message"}  on failure
    """
    server   = sender.get("smtp_server", "").strip()
    port     = int(sender.get("smtp_port", 587))
    email    = sanitize_credential(sender.get("email", ""))
    password = sanitize_credential(sender.get("password", ""))
    use_ssl  = sender.get("use_ssl", port == 465)

    if not server:
        return {"ok": False, "error": "No SMTP server specified."}
    if not email or "@" not in email:
        return {"ok": False, "error": "Invalid email address."}
    if not password:
        return {"ok": False, "error": "Password is empty."}

    # SMTP AUTH is ASCII-only. Check before login so this surfaces as a clear
    # message rather than a UnicodeEncodeError raised inside smtplib.
    for label, value in (("email address", email), ("password", password)):
        bad = non_ascii_report(value)
        if bad:
            return {"ok": False,
                    "error": f"The {label} contains characters that SMTP "
                             f"cannot send: {bad}. {_NON_ASCII_HELP}"}

    try:
        ctx = ssl.create_default_context()
        if use_ssl:
            with smtplib.SMTP_SSL(server, port, context=ctx, timeout=SMTP_TIMEOUT) as s:
                s.login(email, password)
        else:
            with smtplib.SMTP(server, port, timeout=SMTP_TIMEOUT) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(email, password)
        return {"ok": True}

    except smtplib.SMTPAuthenticationError:
        return {
            "ok": False,
            "error": (
                "Authentication failed. "
                "For Gmail use an App Password. "
                "For Hostinger/cPanel use your mailbox password."
            )
        }
    except smtplib.SMTPConnectError as exc:
        return {"ok": False, "error": f"Cannot reach SMTP server ({exc})."}
    except smtplib.SMTPException as exc:
        return {"ok": False, "error": f"SMTP error: {exc}"}
    except OSError as exc:
        return {"ok": False, "error": f"Network error: {exc}"}
    except UnicodeEncodeError as exc:
        # Belt and braces: UnicodeEncodeError is a ValueError, so it is caught
        # by none of the clauses above and would otherwise crash the caller.
        return {"ok": False,
                "error": f"Credentials contain a character SMTP cannot "
                         f"encode ({exc.reason}). {_NON_ASCII_HELP}"}


# ── SMTP SENDER CLASS ──────────────────────────────────────────────────────────

class SMTPSender:
    """
    Manages email sending through a rotating pool of SMTP accounts.
    """

    def __init__(self, senders: list[dict]):
        """
        Args:
            senders: list of sender config dicts (email, password, smtp_server,
                     smtp_port, name, designation, company_name, company_website)
        """
        if not senders:
            raise ValueError("At least one sender account is required.")

        # Per-sender state below is keyed by email address, so two accounts
        # sharing one address would share a send budget and a disable flag.
        # Keep the first occurrence of each address.
        deduped, seen = [], set()
        for s in senders:
            addr = (s.get("email") or "").strip().lower()
            if not addr or addr in seen:
                log_warning(f"Ignoring duplicate or unnamed sender account: {addr or '(blank)'}")
                continue
            seen.add(addr)
            deduped.append(s)

        if not deduped:
            raise ValueError("At least one sender account is required.")

        senders = deduped
        self.senders = senders

        # Per-sender runtime state
        self._send_count:       dict[str, int]  = {s["email"]: 0 for s in senders}
        self._error_streak:     dict[str, int]  = {s["email"]: 0 for s in senders}
        self._disabled:         set[str]         = set()
        self._last_error:       dict[str, str]  = {}

        self._rotation: Iterator[dict] = itertools.cycle(senders)

    # ── SENDER SELECTION ────────────────────────────────────────────────────

    def _is_available(self, sender: dict) -> bool:
        email = sender["email"]
        if email in self._disabled:
            return False
        if self._send_count.get(email, 0) >= MAX_EMAILS_PER_SENDER:
            return False
        return True

    def _get_next_sender(self) -> dict | None:
        """
        Return the next available sender in the rotation.
        Returns None when all senders are exhausted or disabled.
        """
        checked = 0
        while checked < len(self.senders):
            sender = next(self._rotation)
            if self._is_available(sender):
                return sender
            checked += 1
        log_error(
            "All sender accounts are either disabled or have reached their send limit."
        )
        return None

    def _record_success(self, email: str) -> None:
        self._send_count[email] = self._send_count.get(email, 0) + 1
        self._error_streak[email] = 0

    def _record_failure(self, email: str, error: str) -> None:
        self._error_streak[email] = self._error_streak.get(email, 0) + 1
        self._last_error[email] = error
        if self._error_streak[email] >= MAX_CONSECUTIVE_ERRORS:
            self._disabled.add(email)
            log_error(
                f"Sender {email} disabled after {MAX_CONSECUTIVE_ERRORS} "
                f"consecutive failures. Last error: {error}"
            )

    # ── EMAIL CONSTRUCTION ───────────────────────────────────────────────────

    def _build_message(self, sender: dict, to_email: str,
                       subject: str, body: str) -> MIMEMultipart:
        """Build a fully-formed RFC 5322-compliant MIME email."""
        msg = MIMEMultipart("alternative")

        display_name = sender.get("name", sender["email"])
        msg["From"]    = f"{display_name} <{sender['email']}>"
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg["Date"]    = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=sender["email"].split("@")[-1])

        # Unsubscribe header — reduces spam score significantly
        msg["List-Unsubscribe"] = (
            f"<mailto:{sender['email']}?subject=Unsubscribe>"
        )
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        signature = build_signature(
            sender_name=sender.get("name", ""),
            designation=sender.get("designation", ""),
            company_name=sender.get("company_name", ""),
            email=sender["email"],
            company_website=sender.get("company_website", ""),
        )

        # ── Plain text part ──────────────────────────────────────────────
        plain_text = body + "\n\n" + signature
        msg.attach(MIMEText(plain_text, "plain", "utf-8"))

        # ── HTML part ───────────────────────────────────────────────────
        safe_body = html.escape(body).replace("\n", "<br>\n")
        safe_sig  = html.escape(signature).replace("\n", "<br>\n")
        html_body = (
            "<!DOCTYPE html><html><body style='font-family:Arial,sans-serif;"
            "font-size:14px;color:#222;max-width:600px;'>\n"
            f"<p style='line-height:1.6;'>{safe_body}</p>\n"
            "<hr style='border:none;border-top:1px solid #ddd;margin:20px 0;'>\n"
            f"<p style='font-size:12px;color:#777;line-height:1.5;'>{safe_sig}</p>\n"
            "</body></html>"
        )
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        return msg

    # ── SMTP DISPATCH ────────────────────────────────────────────────────────

    def _smtp_send(self, sender: dict, to_email: str,
                   msg: MIMEMultipart) -> dict:
        """
        Open an SMTP connection, authenticate, and deliver the message.
        Returns {"success": bool, "sender_email": str, "error": str|None}.
        """
        server   = sender.get("smtp_server", "").strip()
        port     = int(sender.get("smtp_port", 587))
        use_ssl  = sender.get("use_ssl", port == 465)
        email    = sanitize_credential(sender["email"])
        password = sanitize_credential(sender["password"])

        if not server:
            return {"success": False, "sender_email": email,
                    "error": "No SMTP server configured."}

        bad = non_ascii_report(email) or non_ascii_report(password)
        if bad:
            err = (f"Credentials for {email} contain characters SMTP cannot "
                   f"send: {bad}. {_NON_ASCII_HELP}")
            log_error(err)
            return {"success": False, "sender_email": email, "error": err}

        log_info(
            f"📤 {email} → {to_email}  "
            f"({server}:{port} {'SSL' if use_ssl else 'STARTTLS'})"
        )

        ctx = ssl.create_default_context()
        try:
            if use_ssl:
                with smtplib.SMTP_SSL(server, port, context=ctx,
                                       timeout=SMTP_TIMEOUT) as s:
                    s.login(email, password)
                    s.sendmail(email, to_email, msg.as_bytes())
            else:
                with smtplib.SMTP(server, port, timeout=SMTP_TIMEOUT) as s:
                    s.ehlo()
                    s.starttls(context=ctx)
                    s.ehlo()
                    s.login(email, password)
                    s.sendmail(email, to_email, msg.as_bytes())

            return {"success": True, "sender_email": email, "error": None}

        except smtplib.SMTPAuthenticationError:
            err = (
                f"Authentication failed for {email}. "
                "Use an App Password for Gmail/Yahoo. "
                "For Hostinger/cPanel use your mailbox password."
            )
            log_error(err)
            return {"success": False, "sender_email": email, "error": err}

        except smtplib.SMTPRecipientsRefused:
            # The recipient address was rejected by the server — mark as failed
            # but don't penalise the sender (it's not their fault)
            err = f"Recipient {to_email} was refused by the server."
            log_warning(err)
            return {"success": False, "sender_email": email, "error": err,
                    "_recipient_fault": True}

        except (smtplib.SMTPException, OSError) as exc:
            err = f"SMTP error: {exc}"
            log_error(err)
            return {"success": False, "sender_email": email, "error": err}

        except UnicodeEncodeError as exc:
            # A ValueError, so not covered above — without this a single bad
            # character would abort the whole campaign mid-send.
            err = (f"Cannot encode credentials or message for {email} "
                   f"({exc.reason}). {_NON_ASCII_HELP}")
            log_error(err)
            return {"success": False, "sender_email": email, "error": err}

    # ── PUBLIC API ───────────────────────────────────────────────────────────

    def send_email(self, to_email: str, subject: str, body: str,
                   company_name: str = "") -> dict:
        """
        Send one email using the next available sender.

        Returns:
            {"success": True,  "sender_email": "...", "sender_name": "..."}
            {"success": False, "sender_email": "...", "error": "..."}
        """
        sender = self._get_next_sender()
        if not sender:
            return {"success": False, "error": "No available senders."}

        msg    = self._build_message(sender, to_email, subject, body)
        result = self._smtp_send(sender, to_email, msg)

        if result["success"]:
            self._record_success(sender["email"])
            result["sender_name"] = sender.get("name", sender["email"])
            # Surfaced so the caller can store it; an inbound reply's
            # In-Reply-To header is then an exact match back to this email.
            result["message_id"] = msg.get("Message-ID", "")
            log_success(
                f"Sent to {to_email} "
                f"(#{self._send_count[sender['email']]} from {sender['email']})"
            )
        else:
            if not result.get("_recipient_fault"):
                self._record_failure(sender["email"], result.get("error", ""))

        return result

    def send_followup(self, to_email: str, subject: str, body: str,
                      original_sender_email: str) -> dict:
        """
        Send a follow-up, preferring the original sender for thread continuity.
        Falls back to rotation if the original sender is unavailable.
        """
        # Find original sender in pool
        original = next(
            (s for s in self.senders
             if s["email"] == original_sender_email
             and self._is_available(s)),
            None,
        )

        if original:
            msg    = self._build_message(original, to_email, subject, body)
            result = self._smtp_send(original, to_email, msg)
            if result["success"]:
                self._record_success(original["email"])
                result["sender_name"] = original.get("name", original["email"])
            else:
                self._record_failure(original["email"], result.get("error", ""))
            return result

        # Fall back to normal rotation
        log_warning(
            f"Original sender {original_sender_email} unavailable; "
            "using rotation for follow-up."
        )
        return self.send_email(to_email, subject, body)

    def send_direct(self, sender: dict, to_email: str, subject: str,
                    body: str, in_reply_to: str = "") -> dict:
        """
        Send from one specific account, bypassing rotation.

        Used to answer a prospect from the same mailbox their reply arrived
        in — replying from a different address would look like a new cold
        email and break the thread. *in_reply_to* sets the threading headers
        so the response nests under the existing conversation.
        """
        msg = self._build_message(sender, to_email, subject, body)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"]  = in_reply_to
        # A human answering a reply is not bulk mail.
        del msg["List-Unsubscribe"]
        del msg["List-Unsubscribe-Post"]

        result = self._smtp_send(sender, to_email, msg)
        if result["success"]:
            self._record_success(sender["email"])
            result["message_id"] = msg.get("Message-ID", "")
            log_success(f"Replied to {to_email} from {sender['email']}")
        return result

    def get_stats(self) -> dict:
        """Return a summary of send counts and disabled senders."""
        return {
            "send_counts": dict(self._send_count),
            "disabled_senders": list(self._disabled),
            "error_streaks": dict(self._error_streak),
        }
