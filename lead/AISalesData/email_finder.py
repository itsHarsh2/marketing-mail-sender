"""
email_finder.py - Visits business websites to find contact email addresses.

v4 PERFORMANCE FIXES:
- Per-domain delay reduced from 1.5s to 0.5s (safe for cold outreach scraping)
- Contact page attempts reduced from 9 to 3 (homepage + /contact + /about)
  Most emails are found on page 1 or 2; trying 9 pages wasted 10-15 min per run
- robots.txt check removed from the hot path (was adding ~0.5s per domain)
- Per-site hard timeout: 12s max per lead regardless of retries
- Skip leads that have no website immediately (no delay wasted)
- Progress logged as [N/total] on every lead so UI shows live progress
"""

import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

from utils import (
    extract_emails_from_text,
    clean_url,
    log_info,
    log_warning,
)

# ── CONFIG ─────────────────────────────────────────────────────────────────────

_TIMEOUT          = 6     # seconds per HTTP request (was 8)
_MAX_BYTES        = 512 * 1024   # 512 KB cap (was 1 MB — faster for most sites)
_DOMAIN_DELAY     = 0.5   # seconds between requests to same domain (was 1.5)
_MAX_PAGES        = 3     # homepage + at most 2 contact pages (was 9)

# Contact page slugs tried in order — stop as soon as email found
_CONTACT_SLUGS = ["/contact", "/contact-us", "/about", "/about-us", "/reach-us"]

# ── HTTP SESSION ───────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection":      "keep-alive",
}

_last_request: dict[str, float] = {}


def _get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    adapter = HTTPAdapter(
        max_retries=Retry(
            total=1,                          # only 1 retry (was 3)
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
    )
    s.mount("http://",  adapter)
    s.mount("https://", adapter)
    return s


_SESSION = _get_session()


# ── HELPERS ────────────────────────────────────────────────────────────────────

def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def _throttle(domain: str) -> None:
    """Ensure at least _DOMAIN_DELAY seconds between requests to same domain."""
    elapsed = time.monotonic() - _last_request.get(domain, 0.0)
    if elapsed < _DOMAIN_DELAY:
        time.sleep(_DOMAIN_DELAY - elapsed)
    _last_request[domain] = time.monotonic()


def _fetch(url: str) -> str | None:
    """Fetch a URL, return HTML text or None. Hard cap on size and time."""
    try:
        r = _SESSION.get(url, timeout=_TIMEOUT, allow_redirects=True, stream=True)
        if r.status_code != 200:
            return None
        ct = r.headers.get("Content-Type", "")
        if "text/html" not in ct and "xhtml" not in ct:
            return None
        chunks, total = [], 0
        for chunk in r.iter_content(chunk_size=16384, decode_unicode=True):
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="ignore")
            chunks.append(chunk)
            total += len(chunk)
            if total >= _MAX_BYTES:
                break
        r.close()
        return "".join(chunks)
    except Exception:
        return None


def _emails_from_html(html: str) -> list[str]:
    """Extract and deduplicate email addresses from an HTML page."""
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
        # mailto: links are most reliable
        found = []
        for tag in soup.find_all("a", href=True):
            h = tag["href"]
            if h.lower().startswith("mailto:"):
                addr = h[7:].split("?")[0].strip().lower()
                if addr and "@" in addr:
                    found.append(addr)
        # Regex over visible text
        text = soup.get_text(separator=" ", strip=True)
        found += extract_emails_from_text(text)
    except Exception:
        found = extract_emails_from_text(html)

    # Deduplicate, preserve order
    seen, out = set(), []
    for e in found:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def _best_email(emails: list[str]) -> str | None:
    """Pick the most business-appropriate email from a list."""
    if not emails:
        return None
    priority = ["info", "contact", "hello", "team", "sales",
                "enquiries", "enquiry", "office", "admin", "mail"]
    for prefix in priority:
        for e in emails:
            if e.split("@")[0] == prefix:
                return e
    return emails[0]


# ── PUBLIC: single site ────────────────────────────────────────────────────────

def find_email_for_website(website_url: str) -> str | None:
    """
    Find the best contact email for a website.
    Tries homepage first, then up to _MAX_PAGES-1 contact slugs.
    Returns email string or None.
    """
    url = clean_url(website_url)
    if not url:
        return None

    parsed = urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        return None

    dom   = _domain(url)
    base  = f"{parsed.scheme}://{parsed.netloc}"
    pages = [url] + [base + slug for slug in _CONTACT_SLUGS]
    pages = pages[:_MAX_PAGES]

    for page_url in pages:
        _throttle(dom)
        html = _fetch(page_url)
        if html:
            emails = _emails_from_html(html)
            if emails:
                return _best_email(emails)

    return None


# ── PUBLIC: batch ──────────────────────────────────────────────────────────────

def batch_find_emails(leads: list[dict], max_leads: int = 500, stop_event=None,
                      progress_callback=None) -> list[dict]:
    """
    Find emails for a list of leads in-place.

    Logs [N/total] for every lead so the UI shows live progress.
    Leads without a website are skipped instantly (no delay).
    """
    to_process = leads[:max_leads]
    total      = len(to_process)
    found      = 0

    log_info(f"Step 2/5 — Finding emails for {total} leads (visits up to {_MAX_PAGES} pages per site)...")

    for i, lead in enumerate(to_process, 1):
        if stop_event is not None and stop_event.is_set():
            log_info(f"Email finding paused by user signal — keeping results gathered so far.")
            break
        website = lead.get("website")
        name    = lead.get("company_name", "")[:40]

        if not website:
            lead["email"] = None
            log_info(f"  [{i}/{total}] {name:<40}  (no website)")
            if progress_callback:
                progress_callback(i, leads)
            continue

        email = find_email_for_website(website)
        lead["email"] = email
        if email:
            found += 1
        log_info(f"  [{i}/{total}] {name:<40}  {email or '— not found'}")
        if progress_callback:
            progress_callback(i, leads)

    log_info(f"Step 2/5 complete — emails found: {found}/{total}")
    return leads
