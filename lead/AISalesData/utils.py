"""
utils.py - Shared helper functions used across the whole system.

IMPROVEMENTS v2:
- Structured logging with file output (logs/app.log)
- retry() decorator for automatic retries with backoff
- Thread-safe log capture for the UI
- Email validation tightened
- URL normalisation handles edge cases
- Signature builder sanitises inputs
"""

import re
import time
import random
import logging
import os
import threading
from datetime import datetime
from collections import deque

# ── LOGGING SETUP ─────────────────────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)

_log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger("sales_ai")
logger.setLevel(logging.DEBUG)

# Console handler
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(_log_formatter)
logger.addHandler(_console_handler)

# Rotating file handler — keeps logs across restarts
try:
    from logging.handlers import RotatingFileHandler
    _file_handler = RotatingFileHandler(
        "logs/app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(_log_formatter)
    logger.addHandler(_file_handler)
except Exception:
    pass  # Non-fatal if log file can't be opened

# Thread-safe in-memory log buffer so the Streamlit UI can read logs live
_log_lock = threading.Lock()
_log_buffer: deque = deque(maxlen=300)


class _UILogHandler(logging.Handler):
    """Captures log records into an in-memory deque for the Streamlit UI."""
    def emit(self, record):
        msg = self.format(record)
        with _log_lock:
            _log_buffer.append(msg)


_ui_handler = _UILogHandler()
_ui_handler.setLevel(logging.INFO)
_ui_handler.setFormatter(_log_formatter)
logger.addHandler(_ui_handler)


def get_recent_logs(n: int = 50) -> list[str]:
    """Return the last *n* log lines for display in the UI."""
    with _log_lock:
        lines = list(_log_buffer)
    return lines[-n:]


def clear_log_buffer():
    """Clear the in-memory log buffer (call at campaign start)."""
    with _log_lock:
        _log_buffer.clear()


# Convenience wrappers
def log_info(msg: str):    logger.info(msg)
def log_error(msg: str):   logger.error(msg)
def log_success(msg: str): logger.info("✅ %s", msg)
def log_warning(msg: str): logger.warning("⚠️  %s", msg)
def log_debug(msg: str):   logger.debug(msg)


# ── RETRY DECORATOR ────────────────────────────────────────────────────────────

def retry(max_attempts: int = 3, base_delay: float = 2.0,
          exceptions: tuple = (Exception,)):
    """
    Decorator that retries a function on failure with exponential backoff.

    Usage:
        @retry(max_attempts=3, base_delay=1.5, exceptions=(requests.RequestException,))
        def fetch_page(url): ...
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_attempts:
                        delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                        log_warning(
                            f"{func.__name__} failed (attempt {attempt}/{max_attempts}): "
                            f"{exc}. Retrying in {delay:.1f}s..."
                        )
                        time.sleep(delay)
                    else:
                        log_error(
                            f"{func.__name__} failed after {max_attempts} attempts: {exc}"
                        )
            raise last_exc
        wrapper.__name__ = func.__name__
        return wrapper
    return decorator


# ── CREDENTIAL SANITISING ──────────────────────────────────────────────────────
# SMTP AUTH is ASCII-only (smtplib does initial_response.encode("ascii")), so a
# single invisible character in a password raises UnicodeEncodeError deep inside
# smtplib instead of a useful login error. Copying a Gmail app password out of
# the browser is the usual source: Google renders it as "abcd efgh ijkl mnop"
# and the separators often come across as U+00A0 non-breaking spaces.

_UNICODE_SPACE_CHARS = (
    " "  # no-break space  ← the common Gmail app-password culprit
    "            "
    "  　"
)
_ZERO_WIDTH_CHARS = "​‌‍⁠﻿"

_CREDENTIAL_TRANSLATION = {ord(c): " " for c in _UNICODE_SPACE_CHARS}
_CREDENTIAL_TRANSLATION.update({ord(c): None for c in _ZERO_WIDTH_CHARS})


def sanitize_credential(value: str | None) -> str:
    """
    Make a pasted credential safe for SMTP AUTH.

    Converts exotic Unicode spaces to plain spaces, drops zero-width
    characters, and trims the ends. Regular interior spaces are preserved —
    some providers genuinely allow them in passwords.
    """
    if not value:
        return ""
    return str(value).translate(_CREDENTIAL_TRANSLATION).strip()


def non_ascii_report(value: str | None) -> str:
    """
    Describe any characters that SMTP AUTH cannot encode.

    Returns "" when the value is safe to send, otherwise a readable list
    such as "'é' (U+00E9)" for use in an error message.
    """
    if not value:
        return ""
    bad = sorted({c for c in str(value) if ord(c) > 127})
    return ", ".join(f"{c!r} (U+{ord(c):04X})" for c in bad)


# ── EMAIL HELPERS ──────────────────────────────────────────────────────────────

# Compiled once for performance
_EMAIL_REGEX = re.compile(
    r"(?<![='\"])([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})"
)

_JUNK_PREFIXES = frozenset([
    "noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster",
    "bounce", "notifications", "alerts", "daemon", "filler", "test",
    "placeholder", "sample", "noemail", "nomail", "dummy", "fake",
])
_JUNK_DOMAINS = frozenset([
    "godaddy.com", "mysite.com", "example.com", "domain.com", "wix.com",
    "wixsite.com", "squarespace.com", "weebly.com", "wordpress.com", "wordpress.org",
    "shopify.com", "shopifyemail.com", "webflow.com", "webador.com", "jimdo.com",
    "godaddysites.com", "secureserver.net", "domainmarket.com", "placeholder.com",
    "test.com", "testdomain.com", "sample.com", "sampledomain.com",
    "yourdomain.com", "yourcompany.com", "company.com", "business.com",
    "website.com", "websitebuilder.com", "email.com", "nomail.com", "noemail.com",
    "sentry.io", "wixpress.com",
])
_JUNK_EXTENSIONS = frozenset([".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"])


def extract_emails_from_text(text: str) -> list[str]:
    """
    Find all valid-looking email addresses in a block of text.

    Filters out:
    - no-reply / system addresses
    - known platform domains
    - addresses that end in image extensions (common false positives)
    - duplicate addresses (case-insensitive)
    """
    raw = _EMAIL_REGEX.findall(text)
    seen: set[str] = set()
    result: list[str] = []

    for email in raw:
        email = email.lower().strip().rstrip(".,;:)")
        if email in seen:
            continue

        # Must contain a dot in domain part
        parts = email.split("@")
        if len(parts) != 2 or "." not in parts[1]:
            continue

        prefix, domain = parts[0], parts[1]

        # Skip junk
        if prefix in _JUNK_PREFIXES:
            continue
        if any(prefix.startswith(j) for j in _JUNK_PREFIXES):
            continue
        if domain in _JUNK_DOMAINS:
            continue
        if any(email.endswith(ext) for ext in _JUNK_EXTENSIONS):
            continue

        seen.add(email)
        result.append(email)

    return result


# ── URL HELPERS ────────────────────────────────────────────────────────────────

def clean_url(url: str | None) -> str | None:
    """
    Normalise a URL: strip whitespace, ensure scheme, remove fragments.

    Returns None for obviously invalid inputs.
    """
    if not url:
        return None
    url = url.strip()

    # Remove common noise that maps sometimes appends
    for noise in ["?utm_source=", "&utm_", "#"]:
        if noise in url:
            url = url.split(noise)[0]

    if url in ("http://", "https://", "/", ""):
        return None

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    return url


def get_contact_page_urls(base_url: str) -> list[str]:
    """
    Return a prioritised list of contact-page candidates for a given base URL.
    """
    base = base_url.rstrip("/")
    return [
        f"{base}/contact",
        f"{base}/contact-us",
        f"{base}/contact-us/",
        f"{base}/about",
        f"{base}/about-us",
        f"{base}/get-in-touch",
        f"{base}/reach-us",
        f"{base}/help",
        f"{base}/support",
    ]


# ── MISC ───────────────────────────────────────────────────────────────────────

def random_delay(min_seconds: float = 1.0, max_seconds: float = 4.0):
    """Sleep for a random duration to simulate human behaviour."""
    delay = random.uniform(min_seconds, max_seconds)
    time.sleep(delay)


def build_signature(sender_name: str, designation: str,
                    company_name: str, email: str,
                    company_website: str) -> str:
    """
    Build a clean plain-text email signature.
    Sanitises all inputs and omits blank lines.
    """
    parts = [
        "--",
        (sender_name or "").strip(),
        (designation or "").strip(),
        (company_name or "").strip(),
        (email or "").strip(),
        (company_website or "").strip(),
    ]
    # Drop empty parts (except the separator)
    lines = [parts[0]] + [p for p in parts[1:] if p]
    return "\n".join(lines)
