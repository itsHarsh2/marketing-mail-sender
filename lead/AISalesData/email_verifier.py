"""
email_verifier.py - Checks email domain validity via DNS MX lookup.

IMPROVEMENTS v2:
- Domain-level LRU cache — same domain never looked up twice in one run
- Configurable DNS resolver with custom timeout and public fallback servers
- Concurrent batch verification using ThreadPoolExecutor
- Disposable-email-provider blocklist
- Distinguishes between "no MX record" and "DNS timeout" in the log
- Returns rich result objects instead of bare True/False
"""

import concurrent.futures
import functools
import re
from dataclasses import dataclass, field

import dns.resolver
import dns.exception

from utils import log_info, log_warning, log_error

# ── CONFIG ─────────────────────────────────────────────────────────────────────

# Public DNS servers (used as fallback if the system resolver is flaky)
_DNS_SERVERS = ["8.8.8.8", "1.1.1.1", "8.8.4.4"]
_DNS_TIMEOUT = 5   # seconds per MX query
_BATCH_WORKERS = 8  # parallel threads for batch verification

# Well-known disposable / throwaway email providers
_DISPOSABLE_DOMAINS = frozenset([
    "mailinator.com", "guerrillamail.com", "tempmail.com", "10minutemail.com",
    "throwaway.email", "yopmail.com", "sharklasers.com", "guerrillamailblock.com",
    "trashmail.com", "fakeinbox.com", "dispostable.com", "spamgourmet.com",
    "maildrop.cc", "mailnull.com", "spamspot.com", "discard.email",
])

# Placeholder, sample, site-builder, and excluded domains
_EXCLUDED_DOMAINS = frozenset([
    "godaddy.com", "mysite.com", "example.com", "domain.com", "wix.com",
    "wixsite.com", "squarespace.com", "weebly.com", "wordpress.com", "wordpress.org",
    "shopify.com", "shopifyemail.com", "webflow.com", "webador.com", "jimdo.com",
    "godaddysites.com", "secureserver.net", "domainmarket.com", "placeholder.com",
    "test.com", "testdomain.com", "sample.com", "sampledomain.com",
    "yourdomain.com", "yourcompany.com", "company.com", "business.com",
    "website.com", "websitebuilder.com", "email.com", "nomail.com", "noemail.com",
])

_FILLER_PREFIXES = frozenset([
    "filler", "test", "placeholder", "sample", "noreply", "no-reply",
    "donotreply", "noemail", "nomail", "dummy", "fake",
])

# ── DNS RESOLVER ───────────────────────────────────────────────────────────────

def _make_resolver() -> dns.resolver.Resolver:
    resolver = dns.resolver.Resolver()
    resolver.nameservers = _DNS_SERVERS
    resolver.timeout = _DNS_TIMEOUT
    resolver.lifetime = _DNS_TIMEOUT * 2
    return resolver


_resolver = _make_resolver()


# ── RESULT TYPE ────────────────────────────────────────────────────────────────

@dataclass
class VerificationResult:
    email: str
    valid: bool
    reason: str = ""          # human-readable explanation if invalid

    def __bool__(self) -> bool:
        return self.valid


# ── CORE LOGIC ─────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1024)
def _check_domain(domain: str) -> tuple[bool, str]:
    """
    Cached MX-record lookup for a single domain.

    Returns (is_valid, reason_string).
    The result is cached for the lifetime of the process.
    """
    domain = domain.lower().strip()

    if domain in _DISPOSABLE_DOMAINS:
        return False, "disposable email provider"
    if domain in _EXCLUDED_DOMAINS:
        return False, f"excluded builder / placeholder domain ({domain})"

    try:
        records = _resolver.resolve(domain, "MX")
        if records:
            return True, "ok"
        return False, "no MX records returned"

    except dns.resolver.NXDOMAIN:
        return False, "domain does not exist (NXDOMAIN)"
    except dns.resolver.NoAnswer:
        return False, "no MX record for domain"
    except dns.resolver.NoNameservers:
        return False, "no nameservers available for domain"
    except dns.exception.Timeout:
        # Treat timeout as unknown — we don't want to silently drop leads
        # because of a slow DNS response; log it and allow through
        log_warning(f"DNS timeout checking {domain} — allowing through.")
        return True, "dns_timeout_allowed"
    except Exception as exc:
        log_warning(f"Unexpected DNS error for {domain}: {exc}")
        return True, f"dns_error_allowed: {exc}"


def verify_email(email: str) -> VerificationResult:
    """
    Verify a single email address.

    Checks:
    1. Basic format sanity
    2. Domain is not a known disposable provider
    3. Domain has MX records

    Returns a VerificationResult (truthy if valid).
    """
    if not email or "@" not in email:
        return VerificationResult(email=email or "", valid=False,
                                  reason="not a valid email address")

    parts = email.strip().lower().split("@")
    if len(parts) != 2:
        return VerificationResult(email=email, valid=False, reason="malformed email")

    local, domain = parts

    # Basic local-part sanity & filler prefix check
    if not local or len(local) > 64:
        return VerificationResult(email=email, valid=False, reason="invalid local part")

    if local in _FILLER_PREFIXES or any(local.startswith(p) for p in ("filler", "test", "placeholder", "sample", "dummy")):
        return VerificationResult(email=email, valid=False, reason=f"filler/placeholder prefix ({local})")

    # Domain must have at least one dot
    if "." not in domain or len(domain) > 253:
        return VerificationResult(email=email, valid=False, reason="invalid domain")

    is_valid, reason = _check_domain(domain)
    result = VerificationResult(email=email, valid=is_valid, reason=reason)

    if is_valid:
        log_info(f"  ✅ {email} ({reason})")
    else:
        log_warning(f"  ❌ {email} — {reason}")

    return result


def verify_email_batch(emails: list[str],
                       workers: int = _BATCH_WORKERS) -> list[VerificationResult]:
    """
    Verify many emails concurrently.

    Args:
        emails  : list of email strings
        workers : number of parallel threads

    Returns:
        List of VerificationResult objects in the same order as input.
    """
    if not emails:
        return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(verify_email, emails))

    valid_count = sum(1 for r in results if r.valid)
    log_info(f"Batch verification: {valid_count}/{len(emails)} valid")
    return results


def filter_leads_by_valid_email(
    leads: list[dict],
    workers: int = _BATCH_WORKERS,
) -> tuple[list[dict], list[dict]]:
    """
    Split leads into (valid_leads, invalid_leads) based on email verification.

    Leads without an email are always placed in invalid_leads.

    Args:
        leads   : list of dicts, each with an 'email' key
        workers : parallel DNS threads

    Returns:
        (valid_leads, invalid_leads)
    """
    emails = [lead.get("email") or "" for lead in leads]
    results = verify_email_batch(emails, workers=workers)

    valid_leads: list[dict] = []
    invalid_leads: list[dict] = []

    for lead, res in zip(leads, results):
        lead["email_verified"] = res.valid
        lead["email_verify_reason"] = res.reason
        if res.valid:
            valid_leads.append(lead)
        else:
            invalid_leads.append(lead)

    log_info(
        f"✅ Valid: {len(valid_leads)}  |  "
        f"❌ Invalid/no email: {len(invalid_leads)}"
    )
    return valid_leads, invalid_leads


# ── CLI TEST ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_emails = [
        "hello@google.com",
        "info@stripe.com",
        "fake@notarealemailxyz999.biz",
        "test@mailinator.com",     # disposable
        "bad-email",               # malformed
    ]
    results = verify_email_batch(test_emails)
    for r in results:
        status = "VALID" if r.valid else "INVALID"
        print(f"{r.email:<40} {status:<8} ({r.reason})")
