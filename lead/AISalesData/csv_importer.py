"""
csv_importer.py  —  Import a contact list from CSV or Excel.

Accepted column names (case-insensitive, any order):
    email       → required
    first_name  → recommended  (also accepts: firstname, first)
    last_name   → optional     (also accepts: lastname, last)
    company     → optional     (also accepts: company_name, organisation, organization)

Returns a list of dicts, each with keys:
    email, first_name, last_name, company_name
"""

import io
import re
from typing import Optional

import pandas as pd

from smtp_sender import PROVIDER_PRESETS
from utils import log_info, log_warning, log_error, sanitize_credential


# ── COLUMN NAME ALIASES ────────────────────────────────────────────────────────
# Maps every accepted variant → canonical field name.

_ALIASES = {
    # email
    "email":        "email",
    "email_address":"email",
    "emailaddress": "email",
    "e-mail":       "email",
    "mail":         "email",

    # first_name
    "first_name":   "first_name",
    "firstname":    "first_name",
    "first":        "first_name",
    "fname":        "first_name",
    "given_name":   "first_name",
    "givenname":    "first_name",

    # last_name
    "last_name":    "last_name",
    "lastname":     "last_name",
    "last":         "last_name",
    "lname":        "last_name",
    "surname":      "last_name",
    "family_name":  "last_name",
    "familyname":   "last_name",

    # company
    "company":          "company_name",
    "company_name":     "company_name",
    "companyname":      "company_name",
    "organisation":     "company_name",
    "organization":     "company_name",
    "org":              "company_name",
    "business":         "company_name",
    "business_name":    "company_name",
}

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


# ── SENDER-ACCOUNT COLUMN ALIASES ──────────────────────────────────────────────
# Used by parse_sender_list() to import SMTP sending accounts in bulk.

_SENDER_ALIASES = {
    # email / login
    "email":            "email",
    "email_address":    "email",
    "emailaddress":     "email",
    "e_mail":           "email",
    "username":         "email",
    "user":             "email",
    "login":            "email",

    # password
    "password":         "password",
    "pass":             "password",
    "passwd":           "password",
    "app_password":     "password",
    "apppassword":      "password",
    "smtp_password":    "password",

    # smtp server
    "smtp_server":      "smtp_server",
    "smtpserver":       "smtp_server",
    "smtp":             "smtp_server",
    "smtp_host":        "smtp_server",
    "host":             "smtp_server",
    "server":           "smtp_server",

    # smtp port
    "smtp_port":        "smtp_port",
    "smtpport":         "smtp_port",
    "port":             "smtp_port",

    # ssl
    "use_ssl":          "use_ssl",
    "ssl":              "use_ssl",
    "secure":           "use_ssl",

    # display name
    "name":             "name",
    "sender_name":      "name",
    "from_name":        "name",
    "full_name":        "name",
    "display_name":     "name",

    # job title
    "designation":      "designation",
    "title":            "designation",
    "job_title":        "designation",
    "jobtitle":         "designation",
    "role":             "designation",

    # signature company
    "company":          "company_name",
    "company_name":     "company_name",
    "companyname":      "company_name",
    "organisation":     "company_name",
    "organization":     "company_name",

    # signature website
    "website":          "company_website",
    "company_website":  "company_website",
    "companywebsite":   "company_website",
    "site":             "company_website",
    "url":              "company_website",

    # provider preset name
    "provider":         "provider",
    "service":          "provider",
}

# Email domain → PROVIDER_PRESETS key. Lets a CSV of just email+password work
# for the common consumer providers without an smtp_server column.
_DOMAIN_TO_PROVIDER = {
    "gmail.com":      "Gmail",
    "googlemail.com": "Gmail",
    "outlook.com":    "Outlook / Hotmail",
    "hotmail.com":    "Outlook / Hotmail",
    "live.com":       "Outlook / Hotmail",
    "msn.com":        "Outlook / Hotmail",
    "yahoo.com":      "Yahoo Mail",
    "yahoo.co.uk":    "Yahoo Mail",
    "yahoo.in":       "Yahoo Mail",
    "zoho.com":       "Zoho Mail",
    "zohomail.com":   "Zoho Mail",
    "icloud.com":     "iCloud Mail",
    "me.com":         "iCloud Mail",
    "mac.com":        "iCloud Mail",
    "titan.email":    "Titan Email",
    "hostinger.com":  "Hostinger",
}


def _normalise_provider(name: str) -> Optional[str]:
    """Match a free-text provider name against PROVIDER_PRESETS, loosely."""
    if not name:
        return None
    target = name.strip().lower()
    for preset in PROVIDER_PRESETS:
        if preset.lower() == target:
            return preset
    # Loose match: "gmail" → "Gmail", "outlook" → "Outlook / Hotmail"
    for preset in PROVIDER_PRESETS:
        if preset == "Custom / Other":
            continue
        if target and target in preset.lower():
            return preset
    return None


def _normalise_col(col: str) -> str:
    """Lowercase and strip whitespace/underscores for fuzzy matching."""
    return col.strip().lower().replace(" ", "_").replace("-", "_")


def _map_columns(df: pd.DataFrame, aliases: dict | None = None) -> dict:
    """
    Return a dict mapping canonical_field → actual_df_column for each
    column we recognise.  Unrecognised columns are silently ignored.

    *aliases* defaults to the contact-list alias table; pass
    _SENDER_ALIASES to map a sender-account file instead.
    """
    aliases = _ALIASES if aliases is None else aliases
    mapping = {}
    for col in df.columns:
        alias = _normalise_col(col)
        canonical = aliases.get(alias)
        if canonical and canonical not in mapping:
            mapping[canonical] = col
    return mapping


def _clean_email(val) -> Optional[str]:
    """Return a cleaned, validated email string or None."""
    if pd.isna(val):
        return None
    s = str(val).strip().lower()
    return s if _EMAIL_RE.match(s) else None


def _clean_str(val, default: str = "") -> str:
    """Return a clean string, or *default* if the value is NaN/empty."""
    if pd.isna(val):
        return default
    s = str(val).strip()
    return s if s else default


_TRUTHY = frozenset(["true", "yes", "y", "1", "ssl", "on", "t"])


def _clean_bool(val, default: bool = False) -> bool:
    """Interpret a spreadsheet cell as a boolean (true/yes/y/1/ssl/on)."""
    if pd.isna(val):
        return default
    s = str(val).strip().lower()
    return s in _TRUTHY if s else default


def _read_table(file_bytes: bytes, filename: str) -> Optional[pd.DataFrame]:
    """
    Read an uploaded CSV/Excel file into a DataFrame of strings.

    Handles .xlsx/.xls via openpyxl and falls back from UTF-8 to latin-1
    for CSVs exported from Windows tools.  Returns None if the file
    cannot be read or contains no rows.
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    try:
        buf = io.BytesIO(file_bytes)
        if ext in ("xlsx", "xls"):
            df = pd.read_excel(buf, dtype=str)
        else:
            # Try UTF-8 first, fall back to latin-1 for Windows-exported CSVs
            try:
                df = pd.read_csv(buf, dtype=str, encoding="utf-8")
            except UnicodeDecodeError:
                buf.seek(0)
                df = pd.read_csv(buf, dtype=str, encoding="latin-1")
    except Exception as exc:
        log_error(f"Could not read file '{filename}': {exc}")
        return None

    if df.empty:
        log_warning(f"File '{filename}' is empty.")
        return None

    log_info(f"Loaded '{filename}' — {len(df)} rows, columns: {list(df.columns)}")
    return df


# ── PUBLIC API ─────────────────────────────────────────────────────────────────

def parse_contact_list(file_bytes: bytes, filename: str) -> list[dict]:
    """
    Parse an uploaded CSV or Excel file into a list of contact dicts.

    Args:
        file_bytes : raw bytes from st.file_uploader
        filename   : original file name (used to detect .xlsx vs .csv)

    Returns:
        List of dicts with keys: email, first_name, last_name, company_name
        Only rows with a valid email address are included.
        Duplicates (same email) are removed, keeping the first occurrence.
    """
    df = _read_table(file_bytes, filename)
    if df is None:
        return []

    # ── Map columns ────────────────────────────────────────────────────────
    col_map = _map_columns(df)

    if "email" not in col_map:
        log_error(
            f"No email column found in '{filename}'. "
            f"Columns detected: {list(df.columns)}. "
            "Rename the email column to 'email' and try again."
        )
        return []

    # ── Build contact list ─────────────────────────────────────────────────
    contacts: list[dict] = []
    seen_emails: set[str] = set()
    skipped_invalid = 0
    skipped_dupe    = 0

    for _, row in df.iterrows():
        email = _clean_email(row[col_map["email"]])
        if not email:
            skipped_invalid += 1
            continue
        if email in seen_emails:
            skipped_dupe += 1
            continue
        seen_emails.add(email)

        first_name   = _clean_str(row.get(col_map.get("first_name", ""), ""))
        last_name    = _clean_str(row.get(col_map.get("last_name",  ""), ""))
        company_name = _clean_str(row.get(col_map.get("company_name", ""), ""))

        contacts.append({
            "email":        email,
            "first_name":   first_name,
            "last_name":    last_name,
            "company_name": company_name or "there",   # fallback so emails read naturally
        })

    log_info(
        f"Parsed {len(contacts)} valid contacts "
        f"(skipped {skipped_invalid} invalid email(s), "
        f"{skipped_dupe} duplicate(s))."
    )
    return contacts


def validate_contact_list(contacts: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Light validation pass — returns (valid_contacts, warning_messages).
    Currently checks for missing first_name (generates a warning, not an error).
    """
    warnings: list[str] = []
    no_name = sum(1 for c in contacts if not c.get("first_name"))
    if no_name:
        warnings.append(
            f"{no_name} contact(s) have no first_name — "
            "emails will use 'Hi there' instead of a personal greeting."
        )
    return contacts, warnings


def generate_sample_csv() -> str:
    """Return a sample CSV string the user can download as a template."""
    return (
        "first_name,last_name,email,company\n"
        "John,Smith,john.smith@example.com,Acme Corp\n"
        "Sarah,Jones,sarah.jones@example.com,BrightTech\n"
        "Rahul,Verma,rahul.verma@example.com,StartupXYZ\n"
    )


# ── SENDER ACCOUNTS ────────────────────────────────────────────────────────────

def parse_sender_list(file_bytes: bytes, filename: str) -> tuple[list[dict], dict]:
    """
    Parse an uploaded CSV/Excel file into a list of SMTP sender-account dicts.

    Required columns : email, password
    Optional columns : provider, smtp_server, smtp_port, use_ssl,
                       name, designation, company, website

    When smtp_server is absent it is resolved from the provider column, or
    failing that inferred from the email domain, using PROVIDER_PRESETS.

    Returns:
        (senders, report) where report counts what happened:
        {"total", "imported", "invalid_email", "no_password",
         "no_server", "duplicate"}

    Senders are deduplicated by lowercased email, keeping the first
    occurrence — SMTPSender keys its per-sender send counters and disable
    flags by email, so duplicates would otherwise share one send budget.
    """
    report = {"total": 0, "imported": 0, "invalid_email": 0,
              "no_password": 0, "no_server": 0, "duplicate": 0}

    df = _read_table(file_bytes, filename)
    if df is None:
        return [], report

    report["total"] = len(df)
    col_map = _map_columns(df, _SENDER_ALIASES)

    missing = [f for f in ("email", "password") if f not in col_map]
    if missing:
        log_error(
            f"Sender file '{filename}' is missing required column(s): "
            f"{', '.join(missing)}. Columns detected: {list(df.columns)}."
        )
        return [], report

    def _cell(row, field, default=""):
        """Read a mapped column from a row, or return *default* if absent."""
        col = col_map.get(field)
        return row[col] if col is not None else default

    senders: list[dict] = []
    seen: set[str] = set()

    for _, row in df.iterrows():
        email = _clean_email(_cell(row, "email"))
        if not email:
            report["invalid_email"] += 1
            continue

        # Spreadsheets and copy-paste routinely introduce non-breaking spaces
        # into passwords, which SMTP AUTH cannot encode.
        password = sanitize_credential(_clean_str(_cell(row, "password")))
        if not password:
            report["no_password"] += 1
            continue

        if email in seen:
            report["duplicate"] += 1
            continue

        # ── Resolve SMTP settings ──────────────────────────────────────────
        provider    = _normalise_provider(_clean_str(_cell(row, "provider")))
        smtp_server = _clean_str(_cell(row, "smtp_server"))
        smtp_port   = _clean_str(_cell(row, "smtp_port"))
        has_ssl_col = "use_ssl" in col_map

        preset = PROVIDER_PRESETS.get(provider) if provider else None

        if not smtp_server:
            if not preset:
                # Last resort: infer the provider from the email domain
                domain   = email.rsplit("@", 1)[-1]
                provider = _DOMAIN_TO_PROVIDER.get(domain)
                preset   = PROVIDER_PRESETS.get(provider) if provider else None
            if not preset or not preset.get("smtp_server"):
                report["no_server"] += 1
                log_warning(
                    f"  Skipped {email} — no smtp_server column and provider "
                    "could not be determined from the address."
                )
                continue
            smtp_server = preset["smtp_server"]

        try:
            port = int(float(smtp_port)) if smtp_port else None
        except ValueError:
            port = None
        if port is None:
            port = preset["smtp_port"] if preset else 587

        if has_ssl_col:
            use_ssl = _clean_bool(_cell(row, "use_ssl"), default=(port == 465))
        elif preset:
            use_ssl = bool(preset["use_ssl"])
        else:
            use_ssl = port == 465

        seen.add(email)
        senders.append({
            "email":           email,
            "password":        password,
            "smtp_server":     smtp_server,
            "smtp_port":       port,
            "use_ssl":         use_ssl,
            "name":            _clean_str(_cell(row, "name")),
            "designation":     _clean_str(_cell(row, "designation")),
            "company_name":    _clean_str(_cell(row, "company_name")),
            "company_website": _clean_str(_cell(row, "company_website")),
            "provider":        provider or "Custom / Other",
        })

    report["imported"] = len(senders)
    log_info(
        f"Parsed {report['imported']} sender account(s) from '{filename}' "
        f"(skipped: {report['invalid_email']} invalid email, "
        f"{report['no_password']} missing password, "
        f"{report['no_server']} unresolved SMTP server, "
        f"{report['duplicate']} duplicate)."
    )
    return senders, report


def generate_sender_sample_csv() -> str:
    """
    Return a sample sender-account CSV the user can download as a template.

    Row 1 shows the minimum (email + password — server inferred from the
    domain); the rest show an explicit provider and a fully-specified
    custom mail server.
    """
    return (
        "email,password,provider,smtp_server,smtp_port,use_ssl,"
        "name,designation,company,website\n"
        "john@gmail.com,abcd efgh ijkl mnop,,,,,"
        "John Smith,Sales Manager,Acme Corp,https://acme.com\n"
        "sarah@yourdomain.com,mailboxpassword,Hostinger,,,,"
        "Sarah Jones,Account Executive,Acme Corp,https://acme.com\n"
        "rahul@yourdomain.com,mailboxpassword,,mail.yourdomain.com,465,yes,"
        "Rahul Verma,Partnerships,Acme Corp,https://acme.com\n"
    )


def senders_to_csv(senders: list[dict], include_passwords: bool = False) -> str:
    """
    Serialise the configured sender accounts back to CSV.

    Passwords are omitted unless *include_passwords* is explicitly True —
    an exported file lands in the user's Downloads folder in plain text.
    """
    cols = ["email", "provider", "smtp_server", "smtp_port", "use_ssl",
            "name", "designation", "company_name", "company_website"]
    if include_passwords:
        cols.insert(1, "password")

    rows = [{c: s.get(c, "") for c in cols} for s in senders]
    df = pd.DataFrame(rows, columns=cols)
    df = df.rename(columns={"company_name": "company",
                            "company_website": "website"})
    return df.to_csv(index=False)
