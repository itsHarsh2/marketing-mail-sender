"""
database.py - All database operations using SQLite.

IMPROVEMENTS v2:
- WAL journal mode: concurrent reads don't block writes
- Context-manager helpers prevent connection leaks
- Single-query stats (one SELECT instead of five)
- Schema migrations: adds columns to existing DBs without crashing
- Indexes on hot-path columns (email, sent_at, follow_up_sent)
- DB_PATH configurable via environment variable
- All public functions have explicit type hints
- init_db() is idempotent and safe to call multiple times
"""

import sqlite3
import os
import json
import contextlib
from datetime import datetime, timedelta

# Allow the DB path to be overridden by an environment variable
# (useful for testing or deploying multiple instances)
DB_PATH: str = os.environ.get("SALES_DB_PATH", "sales_campaign.db")


# ── CONNECTION ─────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _db():
    """
    Context manager: yields a (connection, cursor) pair and always closes.

    Usage:
        with _db() as (conn, cur):
            cur.execute(...)
            conn.commit()   # only when you want to write
    """
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL mode: readers never block writers and vice-versa
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn, conn.cursor()
    finally:
        conn.close()


# ── SCHEMA ─────────────────────────────────────────────────────────────────────

_SCHEMA = [
    # Scraped leads ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS leads (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        company_name  TEXT    NOT NULL,
        website       TEXT,
        email         TEXT,
        location      TEXT,
        business_type TEXT,
        scraped_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        email_verified INTEGER NOT NULL DEFAULT 0
    )
    """,
    # Sent emails ───────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS emails_sent (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id          INTEGER REFERENCES leads(id),
        company_name     TEXT,
        recipient_email  TEXT    NOT NULL,
        sender_email     TEXT    NOT NULL,
        subject          TEXT,
        body             TEXT,
        status           TEXT    NOT NULL DEFAULT 'sent',
        sent_at          TEXT    NOT NULL DEFAULT (datetime('now')),
        opened_at        TEXT,
        replied_at       TEXT,
        follow_up_sent   INTEGER NOT NULL DEFAULT 0,
        follow_up_at     TEXT,
        campaign_id      INTEGER REFERENCES campaigns(id)
    )
    """,
    # Saved SMTP sender accounts ────────────────────────────────────────────
    # password_enc holds a Fernet token, never a plaintext password.
    """
    CREATE TABLE IF NOT EXISTS sender_accounts (
        uid             TEXT    PRIMARY KEY,
        email           TEXT    NOT NULL UNIQUE,
        password_enc    BLOB,
        smtp_server     TEXT,
        smtp_port       INTEGER NOT NULL DEFAULT 587,
        use_ssl         INTEGER NOT NULL DEFAULT 0,
        name            TEXT,
        designation     TEXT,
        company_name    TEXT,
        company_website TEXT,
        provider        TEXT,
        sort_order      INTEGER NOT NULL DEFAULT 0,
        updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # Replies received from prospects ───────────────────────────────────────
    # One row per inbound message we matched to an email we sent. mailbox is
    # the sender account whose inbox it arrived in, since replies scatter
    # across the rotation pool.
    """
    CREATE TABLE IF NOT EXISTS replies (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        email_sent_id  INTEGER REFERENCES emails_sent(id),
        lead_id        INTEGER,
        mailbox        TEXT    NOT NULL,
        from_email     TEXT    NOT NULL,
        from_name      TEXT,
        subject        TEXT,
        body           TEXT,
        received_at    TEXT,
        imap_uid       TEXT,
        message_id     TEXT UNIQUE,
        is_read        INTEGER NOT NULL DEFAULT 0,
        handled        INTEGER NOT NULL DEFAULT 0,
        fetched_at     TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # Scheduled sends ───────────────────────────────────────────────────────
    # payload holds the approved emails as JSON. Sender credentials are NOT
    # stored here — the worker reads them from sender_accounts at send time,
    # so a schedule never contains a password.
    """
    CREATE TABLE IF NOT EXISTS scheduled_sends (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        kind          TEXT    NOT NULL DEFAULT 'followup',
        label         TEXT,
        scheduled_for TEXT    NOT NULL,
        payload       TEXT    NOT NULL,
        status        TEXT    NOT NULL DEFAULT 'pending',
        result        TEXT,
        created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        completed_at  TEXT
    )
    """,
    # Campaigns ─────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS campaigns (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        business_type   TEXT,
        location        TEXT,
        offering        TEXT,
        company_name    TEXT,
        company_website TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        status          TEXT    NOT NULL DEFAULT 'running'
    )
    """,
    # Persistent User Sessions ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS user_sessions (
        token       TEXT PRIMARY KEY,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at  TEXT NOT NULL
    )
    """,
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_leads_email         ON leads(email)",
    "CREATE INDEX IF NOT EXISTS idx_leads_verified      ON leads(email_verified)",
    "CREATE INDEX IF NOT EXISTS idx_emails_sent_at      ON emails_sent(sent_at)",
    "CREATE INDEX IF NOT EXISTS idx_emails_followup     ON emails_sent(follow_up_sent)",
    "CREATE INDEX IF NOT EXISTS idx_emails_isfollowup   ON emails_sent(is_followup)",
    "CREATE INDEX IF NOT EXISTS idx_emails_recipient    ON emails_sent(recipient_email)",
    "CREATE INDEX IF NOT EXISTS idx_emails_campaign     ON emails_sent(campaign_id)",
    "CREATE INDEX IF NOT EXISTS idx_sender_order        ON sender_accounts(sort_order)",
    "CREATE INDEX IF NOT EXISTS idx_sched_status        ON scheduled_sends(status, scheduled_for)",
    "CREATE INDEX IF NOT EXISTS idx_replies_unread      ON replies(is_read, received_at)",
    "CREATE INDEX IF NOT EXISTS idx_replies_from        ON replies(from_email)",
    "CREATE INDEX IF NOT EXISTS idx_emails_msgid        ON emails_sent(message_id)",
]

# Columns added after v1 — each tuple is (table, column, definition)
_MIGRATIONS = [
    # Store the Message-ID we send so a reply carrying In-Reply-To can be
    # matched exactly, instead of guessing from the subject line.
    ("emails_sent", "message_id", "TEXT"),
    # Optional per-account IMAP host. The smtp.->imap. guess fails for
    # mail.yourdomain.com style servers, so allow an explicit value.
    ("sender_accounts", "imap_server", "TEXT"),
    # Flag to identify sent follow-up emails vs initial cold emails.
    ("emails_sent", "is_followup", "INTEGER DEFAULT 0"),
    # Campaign pause & resume state columns
    ("campaigns", "unsent_payload", "TEXT"),
    ("campaigns", "sent_count", "INTEGER DEFAULT 0"),
    ("campaigns", "total_count", "INTEGER DEFAULT 0"),
    # Durable pipeline checkpoint.  This lets a campaign survive a browser
    # refresh, process restart, or a pause during scraping/composition.
    ("campaigns", "resume_stage", "TEXT"),
    ("campaigns", "resume_payload", "TEXT"),
]


def init_db() -> None:
    """
    Create tables, apply indexes, and run any pending migrations.
    Safe to call multiple times (all statements use IF NOT EXISTS).
    """
    with _db() as (conn, cur):
        for stmt in _SCHEMA:
            cur.execute(stmt)

        # Migrations run BEFORE indexes: an index may reference a column that
        # a migration is about to add, and CREATE INDEX on a missing column
        # fails outright.
        for table, column, definition in _MIGRATIONS:
            cur.execute(f"PRAGMA table_info({table})")
            existing_cols = {row["name"] for row in cur.fetchall()}
            if column not in existing_cols:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        for stmt in _INDEXES:
            cur.execute(stmt)

        conn.commit()


# ── LEADS ──────────────────────────────────────────────────────────────────────

def insert_lead(company_name: str, website: str | None,
                email: str | None, location: str,
                business_type: str) -> int:
    """
    Insert a new lead or return the ID of the matching existing one.

    Deduplication key: same email OR (same company name + same website).
    Returns the lead's database ID.
    """
    with _db() as (conn, cur):
        # Check for duplicate by email first (most reliable key)
        if email:
            cur.execute("SELECT id FROM leads WHERE email = ?", (email,))
            row = cur.fetchone()
            if row:
                return row["id"]

        # Then by name + website combo
        if company_name and website:
            cur.execute(
                "SELECT id FROM leads WHERE company_name = ? AND website = ?",
                (company_name, website),
            )
            row = cur.fetchone()
            if row:
                return row["id"]

        cur.execute(
            """
            INSERT INTO leads (company_name, website, email, location, business_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (company_name, website, email, location, business_type),
        )
        conn.commit()
        return cur.lastrowid


def update_lead_email(lead_id: int, email: str, verified: bool = False) -> None:
    """Update the email address and verification status of a lead."""
    with _db() as (conn, cur):
        cur.execute(
            "UPDATE leads SET email = ?, email_verified = ? WHERE id = ?",
            (email, 1 if verified else 0, lead_id),
        )
        conn.commit()


def get_leads_without_email() -> list[dict]:
    """Return all leads that don't yet have an email address."""
    with _db() as (_, cur):
        cur.execute("SELECT * FROM leads WHERE email IS NULL OR email = ''")
        return [dict(r) for r in cur.fetchall()]


def deduplicate_leads(leads: list[dict], skip_already_sent: bool = True) -> tuple[list[dict], dict]:
    """
    Deduplicate leads by email address.

    1. Removes duplicate emails within the current batch.
    2. (Optional) Excludes emails that were already sent a message in previous campaigns.

    Args:
        leads             : list of dicts, each containing an 'email' key
        skip_already_sent : if True, excludes recipient emails present in emails_sent table

    Returns:
        (unique_leads, stats_dict)
    """
    seen_emails: set[str] = set()
    already_sent_emails: set[str] = set()

    if skip_already_sent:
        try:
            with _db() as (_, cur):
                cur.execute(
                    "SELECT DISTINCT LOWER(recipient_email) FROM emails_sent "
                    "WHERE recipient_email IS NOT NULL AND recipient_email != ''"
                )
                already_sent_emails = {row[0] for row in cur.fetchall()}
        except Exception:
            already_sent_emails = set()

    unique_leads = []
    batch_dupes = 0
    past_sent_dupes = 0

    for lead in leads:
        email = (lead.get("email") or "").strip().lower()
        if not email:
            # Scraper checkpoints can contain leads whose website lookup has
            # not happened yet.  They are not duplicates; keep them so the
            # resumed worker can continue finding their email address.
            unique_leads.append(lead)
            continue

        if email in seen_emails:
            batch_dupes += 1
            continue

        if skip_already_sent and email in already_sent_emails:
            past_sent_dupes += 1
            continue

        seen_emails.add(email)
        unique_leads.append(lead)

    stats = {
        "total": len(leads),
        "kept": len(unique_leads),
        "batch_duplicates": batch_dupes,
        "past_sent_duplicates": past_sent_dupes,
    }

    return unique_leads, stats


# ── EMAILS SENT ────────────────────────────────────────────────────────────────

def has_initial_email_sent(recipient_email: str) -> bool:
    """Return whether this address already received an initial campaign email."""
    email = (recipient_email or "").strip().lower()
    if not email:
        return False

    with _db() as (_, cur):
        cur.execute(
            """
            SELECT 1 FROM emails_sent
            WHERE LOWER(TRIM(recipient_email)) = ?
              AND COALESCE(is_followup, 0) = 0
            LIMIT 1
            """,
            (email,),
        )
        return cur.fetchone() is not None


def insert_email_sent(lead_id: int | None, company_name: str,
                      recipient_email: str, sender_email: str,
                      subject: str, body: str,
                      campaign_id: int | None = None,
                      message_id: str | None = None,
                      is_followup: int = 0) -> int:
    """Record a sent email with local IST timestamp. Returns the new row's ID."""
    now_ist = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db() as (conn, cur):
        cur.execute(
            """
            INSERT INTO emails_sent
                (lead_id, company_name, recipient_email, sender_email,
                 subject, body, campaign_id, message_id, sent_at, is_followup)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (lead_id, company_name, recipient_email, sender_email,
             subject, body, campaign_id, message_id, now_ist, is_followup),
        )
        conn.commit()
        return cur.lastrowid


def mark_follow_up_sent(email_id: int) -> None:
    """Mark a specific sent-email row as having received a follow-up."""
    with _db() as (conn, cur):
        cur.execute(
            """
            UPDATE emails_sent
            SET follow_up_sent = 1, follow_up_at = datetime('now')
            WHERE id = ?
            """,
            (email_id,),
        )
        conn.commit()


def mark_recipient_followed_up(recipient_email: str) -> int:
    """
    Mark every outstanding email to *recipient_email* as followed-up.

    Follow-ups are deduplicated per person, so one message can answer
    several earlier emails to the same address. Marking only the previewed
    row would leave its siblings eligible and the recipient would be
    followed up again on the next run.

    Returns the number of rows updated.
    """
    with _db() as (conn, cur):
        cur.execute(
            """
            UPDATE emails_sent
            SET follow_up_sent = 1, follow_up_at = datetime('now')
            WHERE follow_up_sent = 0
              AND lower(recipient_email) = lower(?)
            """,
            (recipient_email,),
        )
        conn.commit()
        return cur.rowcount


def get_emails_needing_followup(days_ago: int = 3) -> list[dict]:
    """
    Return emails sent at least *days_ago* days ago that:
    - haven't received a follow-up yet for this campaign email
    - haven't had any reply recorded from this recipient
    - are still in 'sent' status
    """
    cutoff_dt = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as (_, cur):
        cur.execute(
            """
            SELECT e.*
            FROM   emails_sent e
            WHERE  e.follow_up_sent = 0
              AND  e.status = 'sent'
              AND  e.replied_at IS NULL
              AND  e.sent_at <= ?
              AND  NOT EXISTS (
                  SELECT 1 FROM replies r
                  WHERE lower(r.from_email) = lower(e.recipient_email)
              )
            """,
            (cutoff_dt,),
        )
        return [dict(r) for r in cur.fetchall()]


# ── CAMPAIGNS ──────────────────────────────────────────────────────────────────

def create_campaign(business_type: str, location: str, offering: str,
                    company_name: str, company_website: str) -> int:
    """Create a new campaign record and return its ID."""
    with _db() as (conn, cur):
        cur.execute(
            """
            INSERT INTO campaigns
                (business_type, location, offering, company_name, company_website)
            VALUES (?, ?, ?, ?, ?)
            """,
            (business_type, location, offering, company_name, company_website),
        )
        conn.commit()
        return cur.lastrowid


def update_campaign_status(campaign_id: int, status: str) -> None:
    """Update the status field of a campaign ('running', 'complete', 'error', 'paused', 'cancelled')."""
    with _db() as (conn, cur):
        cur.execute(
            "UPDATE campaigns SET status = ? WHERE id = ?",
            (status, campaign_id),
        )
        conn.commit()


def save_paused_campaign(campaign_id: int, unsent_records: list[dict], sent_count: int = 0, total_count: int = 0,
                         resume_stage: str = "", resume_payload: dict | None = None) -> None:
    """Save remaining unsent records and set campaign status to 'paused'."""
    payload_json = json.dumps(unsent_records)
    with _db() as (conn, cur):
        cur.execute(
            """
            UPDATE campaigns
            SET status = 'paused', unsent_payload = ?, sent_count = ?, total_count = ?,
                resume_stage = ?, resume_payload = ?
            WHERE id = ?
            """,
            (payload_json, sent_count, total_count, resume_stage,
             json.dumps(resume_payload or {}), campaign_id),
        )
        conn.commit()


def get_paused_campaigns() -> list[dict]:
    """Return all campaigns currently marked as 'paused'."""
    with _db() as (_, cur):
        cur.execute(
            """
            SELECT id, business_type, location, offering, company_name, company_website,
                   created_at, status, unsent_payload, sent_count, total_count,
                   resume_stage, resume_payload
            FROM campaigns
            WHERE status = 'paused'
            ORDER BY id DESC
            """
        )
        rows = [dict(r) for r in cur.fetchall()]
        for row in rows:
            if row.get("unsent_payload"):
                try:
                    row["unsent_records"] = json.loads(row["unsent_payload"])
                except Exception:
                    row["unsent_records"] = []
            else:
                row["unsent_records"] = []
            try:
                row["resume_data"] = json.loads(row.get("resume_payload") or "{}")
            except Exception:
                row["resume_data"] = {}
        return rows


def get_paused_campaign(campaign_id: int) -> dict | None:
    """Return details of a specific paused campaign."""
    with _db() as (_, cur):
        cur.execute(
            """
            SELECT id, business_type, location, offering, company_name, company_website,
                   created_at, status, unsent_payload, sent_count, total_count,
                   resume_stage, resume_payload
            FROM campaigns
            WHERE id = ? AND status = 'paused'
            """,
            (campaign_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        res = dict(row)
        if res.get("unsent_payload"):
            try:
                res["unsent_records"] = json.loads(res["unsent_payload"])
            except Exception:
                res["unsent_records"] = []
        else:
            res["unsent_records"] = []
        try:
            res["resume_data"] = json.loads(res.get("resume_payload") or "{}")
        except Exception:
            res["resume_data"] = {}
        return res


# ── ANALYTICS ─────────────────────────────────────────────────────────────────

def get_campaign_stats(campaign_id: int | None = None) -> dict:
    """
    Return summary counts for the dashboard in a single SQL pass.

    If *campaign_id* is supplied, stats are scoped to that campaign.
    Otherwise, totals across all campaigns are returned.
    """
    if campaign_id:
        where_scope = "WHERE campaign_id = ?"
        params: tuple = (campaign_id,)
    else:
        where_scope = ""
        params = ()

    with _db() as (_, cur):
        # Single-pass aggregation using conditional COUNT
        cur.execute(
            f"""
            SELECT
                COUNT(CASE WHEN is_followup = 0 THEN 1 END)          AS sent,
                COUNT(CASE WHEN opened_at IS NOT NULL THEN 1 END)    AS opened,
                COUNT(CASE WHEN replied_at IS NOT NULL THEN 1 END)   AS replied,
                COUNT(CASE WHEN is_followup = 1 THEN 1 END)          AS follow_ups
            FROM emails_sent
            {where_scope}
            """,
            params,
        )
        row = cur.fetchone()
        sent      = row["sent"]
        opened    = row["opened"]
        replied   = row["replied"]
        followups = row["follow_ups"]

        cur.execute("SELECT COUNT(*) AS n FROM leads")
        total_leads = cur.fetchone()["n"]

        cur.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE email IS NOT NULL AND email != ''"
        )
        leads_with_email = cur.fetchone()["n"]

    return {
        "sent":             sent,
        "opened":           opened,
        "replied":          replied,
        "follow_ups":       followups,
        "total_leads":      total_leads,
        "leads_with_email": leads_with_email,
        "open_rate":        round(opened / sent * 100, 1) if sent else 0.0,
        "reply_rate":       round(replied / sent * 100, 1) if sent else 0.0,
    }


def get_all_emails_sent(limit: int = 500) -> list[dict]:
    """
    Return the most recent *limit* sent emails for the campaign table.
    """
    with _db() as (_, cur):
        cur.execute(
            """
            SELECT  company_name, recipient_email, sender_email,
                    subject, status, sent_at, follow_up_sent, is_followup
            FROM    emails_sent
            ORDER BY sent_at DESC
            LIMIT   ?
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_daily_report_data(target_date: str) -> dict:
    """
    Gather stats, sent emails, replies, and leads for a specific date (YYYY-MM-DD).
    Matches both direct local dates and UTC timezone offset dates for IST (+5:30).
    """
    with _db() as (_, cur):
        # Initial cold emails sent on target_date (is_followup = 0)
        cur.execute(
            """
            SELECT company_name, recipient_email, sender_email,
                   subject, status, sent_at, follow_up_sent, is_followup
            FROM   emails_sent
            WHERE  (date(sent_at) = date(?) OR date(sent_at, '+5 hours', '+30 minutes') = date(?))
              AND  is_followup = 0
            ORDER BY sent_at DESC
            """,
            (target_date, target_date),
        )
        sent_emails = [dict(r) for r in cur.fetchall()]

        # Replies received on target_date
        cur.execute(
            """
            SELECT mailbox, from_email, from_name, subject, body, received_at
            FROM   replies
            WHERE  date(received_at) = date(?)
               OR  date(received_at, '+5 hours', '+30 minutes') = date(?)
            ORDER BY received_at DESC
            """,
            (target_date, target_date),
        )
        replies = [dict(r) for r in cur.fetchall()]

        # Leads scraped on target_date
        cur.execute(
            """
            SELECT COUNT(*) AS n FROM leads
            WHERE date(scraped_at) = date(?)
               OR date(scraped_at, '+5 hours', '+30 minutes') = date(?)
            """,
            (target_date, target_date),
        )
        leads_scraped = cur.fetchone()["n"]

        # Sender account volume breakdown on target_date
        cur.execute(
            """
            SELECT sender_email, COUNT(*) as count
            FROM   emails_sent
            WHERE  date(sent_at) = date(?)
               OR  date(sent_at, '+5 hours', '+30 minutes') = date(?)
            GROUP BY sender_email
            ORDER BY count DESC
            """,
            (target_date, target_date),
        )
        sender_breakdown = [dict(r) for r in cur.fetchall()]

        # Follow-ups sent by us on target_date (is_followup = 1)
        cur.execute(
            """
            SELECT COUNT(*) AS n FROM emails_sent
            WHERE  (date(sent_at) = date(?) OR date(sent_at, '+5 hours', '+30 minutes') = date(?))
              AND  is_followup = 1
            """,
            (target_date, target_date),
        )
        followups_sent = cur.fetchone()["n"]

    return {
        "date":                target_date,
        "total_sent":          len(sent_emails),
        "total_replies":       len(replies),
        "total_followups":     followups_sent,
        "total_leads_scraped": leads_scraped,
        "sent_emails":         sent_emails,
        "replies":             replies,
        "sender_breakdown":    sender_breakdown,
    }


# ── SAVED SENDER ACCOUNTS ─────────────────────────────────────────────────────
# Passwords are encrypted before they reach this layer; see crypto_util.py.
# These functions deliberately take/return the plain sender dicts the rest of
# the app already uses, so callers never handle ciphertext themselves.

_SENDER_COLUMNS = (
    "smtp_server", "smtp_port", "use_ssl", "name",
    "designation", "company_name", "company_website", "provider",
)


def save_sender_accounts(senders: list[dict]) -> int:
    """
    Replace the saved sender accounts with *senders*.

    The in-app list is the source of truth, so this mirrors it exactly:
    accounts removed in the UI disappear from storage too. Accounts with no
    email are skipped. Returns the number of accounts written.
    """
    from crypto_util import encrypt

    rows = []
    for order, s in enumerate(senders):
        email = (s.get("email") or "").strip().lower()
        if not email:
            continue
        rows.append((
            s.get("uid") or email,
            email,
            encrypt(s.get("password") or ""),
            s.get("smtp_server", ""),
            int(s.get("smtp_port", 587) or 587),
            1 if s.get("use_ssl") else 0,
            s.get("name", ""),
            s.get("designation", ""),
            s.get("company_name", ""),
            s.get("company_website", ""),
            s.get("provider", "Custom / Other"),
            order,
        ))

    with _db() as (conn, cur):
        cur.execute("DELETE FROM sender_accounts")
        if rows:
            cur.executemany(
                """
                INSERT INTO sender_accounts
                    (uid, email, password_enc, smtp_server, smtp_port, use_ssl,
                     name, designation, company_name, company_website,
                     provider, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        conn.commit()
    return len(rows)


def load_sender_accounts() -> list[dict]:
    """
    Return saved sender accounts as plain dicts, ready for session state.

    An account whose password cannot be decrypted (rotated or lost key) is
    returned with an empty password rather than being dropped, so the user
    can see the account and re-enter its credential.
    """
    from crypto_util import decrypt

    with _db() as (_, cur):
        cur.execute(
            """
            SELECT uid, email, password_enc, smtp_server, smtp_port, use_ssl,
                   name, designation, company_name, company_website, provider
            FROM   sender_accounts
            ORDER BY sort_order, email
            """
        )
        rows = cur.fetchall()

    senders = []
    for r in rows:
        senders.append({
            "uid":             r["uid"],
            "email":           r["email"],
            "password":        decrypt(r["password_enc"]),
            "smtp_server":     r["smtp_server"] or "",
            "smtp_port":       int(r["smtp_port"] or 587),
            "use_ssl":         bool(r["use_ssl"]),
            "name":            r["name"] or "",
            "designation":     r["designation"] or "",
            "company_name":    r["company_name"] or "",
            "company_website": r["company_website"] or "",
            "provider":        r["provider"] or "Custom / Other",
        })
    return senders


def count_sender_accounts() -> int:
    """Number of saved sender accounts, without decrypting anything."""
    with _db() as (_, cur):
        cur.execute("SELECT COUNT(*) AS n FROM sender_accounts")
        return cur.fetchone()["n"]


def clear_sender_accounts() -> int:
    """Delete every saved sender account. Returns how many were removed."""
    with _db() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM sender_accounts")
        n = cur.fetchone()["n"]
        cur.execute("DELETE FROM sender_accounts")
        conn.commit()
    return n


# ── REPLIES ───────────────────────────────────────────────────────────────────

def get_sent_index() -> list[dict]:
    """
    Everything needed to match an inbound message to an email we sent:
    our Message-ID, the recipient, and the subject.
    """
    with _db() as (_, cur):
        cur.execute(
            """
            SELECT id, lead_id, recipient_email, subject, message_id, sender_email
            FROM   emails_sent
            ORDER BY id DESC
            LIMIT  5000
            """
        )
        return [dict(r) for r in cur.fetchall()]


def insert_reply(email_sent_id, lead_id, mailbox, from_email, from_name,
                 subject, body, received_at, message_id, imap_uid="") -> int | None:
    """
    Record an inbound reply. Returns the new row id, or None if this
    message_id was already stored (re-checking an inbox is idempotent).
    """
    with _db() as (conn, cur):
        try:
            cur.execute(
                """
                INSERT INTO replies
                    (email_sent_id, lead_id, mailbox, from_email, from_name,
                     subject, body, received_at, message_id, imap_uid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (email_sent_id, lead_id, mailbox, from_email, from_name,
                 subject, body, received_at, message_id, imap_uid),
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None   # already have this message


def mark_email_replied(email_sent_id: int) -> None:
    """Flag the original email as replied so follow-ups stop targeting it."""
    with _db() as (conn, cur):
        cur.execute(
            """UPDATE emails_sent
               SET replied_at = datetime('now'), status = 'replied'
               WHERE id = ?""",
            (email_sent_id,),
        )
        conn.commit()


def mark_recipient_replied(recipient_email: str) -> int:
    """
    Flag every outstanding email to this person as replied.

    Someone answering once should stop all pending follow-ups to them, not
    just the single thread they happened to answer.
    """
    with _db() as (conn, cur):
        cur.execute(
            """UPDATE emails_sent
               SET replied_at = datetime('now'), status = 'replied'
               WHERE replied_at IS NULL AND lower(recipient_email) = lower(?)""",
            (recipient_email,),
        )
        conn.commit()
        return cur.rowcount


def get_replies(only_unhandled: bool = False, limit: int = 300) -> list[dict]:
    """Stored replies, newest first."""
    q = """SELECT * FROM replies {} ORDER BY received_at DESC, id DESC LIMIT ?"""
    where = "WHERE handled = 0" if only_unhandled else ""
    with _db() as (_, cur):
        cur.execute(q.format(where), (limit,))
        return [dict(r) for r in cur.fetchall()]


def count_unread_replies() -> int:
    with _db() as (_, cur):
        cur.execute("SELECT COUNT(*) AS n FROM replies WHERE is_read = 0")
        return cur.fetchone()["n"]


def set_reply_flags(reply_id: int, is_read: bool | None = None,
                    handled: bool | None = None) -> None:
    """Update the read / handled flags on a reply."""
    sets, params = [], []
    if is_read is not None:
        sets.append("is_read = ?");  params.append(1 if is_read else 0)
    if handled is not None:
        sets.append("handled = ?");  params.append(1 if handled else 0)
    if not sets:
        return
    params.append(reply_id)
    with _db() as (conn, cur):
        cur.execute(f"UPDATE replies SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()


# ── SCHEDULED SENDS ───────────────────────────────────────────────────────────
# Times are stored as local-time ISO strings ("YYYY-MM-DD HH:MM:SS") and
# compared in Python against datetime.now(). SQLite's datetime('now') is UTC,
# so it is deliberately not used for the due-check.

def create_scheduled_send(kind: str, scheduled_for: str,
                          payload: list[dict], label: str = "") -> int:
    """Queue approved emails to be sent at *scheduled_for*. Returns its ID."""
    import json
    with _db() as (conn, cur):
        cur.execute(
            """
            INSERT INTO scheduled_sends (kind, label, scheduled_for, payload)
            VALUES (?, ?, ?, ?)
            """,
            (kind, label, scheduled_for, json.dumps(payload)),
        )
        conn.commit()
        return cur.lastrowid


def get_due_scheduled_sends(now_iso: str) -> list[dict]:
    """Return pending jobs whose time has arrived, oldest first."""
    import json
    with _db() as (_, cur):
        cur.execute(
            """
            SELECT * FROM scheduled_sends
            WHERE status = 'pending' AND scheduled_for <= ?
            ORDER BY scheduled_for
            """,
            (now_iso,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        try:
            r["payload"] = json.loads(r["payload"])
        except Exception:
            r["payload"] = []
    return rows


def get_scheduled_sends(status: str | None = "pending") -> list[dict]:
    """List scheduled jobs, optionally filtered by status. Payload stays JSON."""
    with _db() as (_, cur):
        if status:
            cur.execute(
                """SELECT id, kind, label, scheduled_for, status, result,
                          created_at, completed_at
                   FROM scheduled_sends WHERE status = ?
                   ORDER BY scheduled_for""",
                (status,),
            )
        else:
            cur.execute(
                """SELECT id, kind, label, scheduled_for, status, result,
                          created_at, completed_at
                   FROM scheduled_sends ORDER BY scheduled_for DESC LIMIT 50"""
            )
        return [dict(r) for r in cur.fetchall()]


def claim_scheduled_send(send_id: int) -> bool:
    """
    Atomically move a job from 'pending' to 'running'.

    Returns True only for the caller that won the race, so two workers (or
    two browser tabs) can never send the same batch twice.
    """
    with _db() as (conn, cur):
        cur.execute(
            "UPDATE scheduled_sends SET status='running' WHERE id=? AND status='pending'",
            (send_id,),
        )
        conn.commit()
        return cur.rowcount == 1


def finish_scheduled_send(send_id: int, status: str, result: str = "") -> None:
    """Mark a job finished ('sent', 'error', ...) with a short result note."""
    with _db() as (conn, cur):
        cur.execute(
            """UPDATE scheduled_sends
               SET status = ?, result = ?, completed_at = datetime('now')
               WHERE id = ?""",
            (status, result[:500], send_id),
        )
        conn.commit()


def cancel_scheduled_send(send_id: int) -> bool:
    """Cancel a job that has not started. Returns True if it was cancelled."""
    with _db() as (conn, cur):
        cur.execute(
            "UPDATE scheduled_sends SET status='cancelled' WHERE id=? AND status='pending'",
            (send_id,),
        )
        conn.commit()
        return cur.rowcount == 1


# ── APP SETTINGS PERSISTENCE ───────────────────────────────────────────────────

def save_setting(key: str, value: str) -> None:
    """Save or replace a single setting key-value pair in the database."""
    with _db() as (conn, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (key, str(value or "")),
        )
        conn.commit()


def load_settings() -> dict:
    """Load all saved settings from the database as a dictionary."""
    with _db() as (conn, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("SELECT key, value FROM app_settings")
        return {row["key"]: row["value"] for row in cur.fetchall()}


# ── SESSION PERSISTENCE ────────────────────────────────────────────────────────

def create_user_session(token: str, hours: int = 24) -> None:
    """Save a session token valid for specified hours into SQLite."""
    with _db() as (conn, cur):
        cur.execute(
            """
            INSERT OR REPLACE INTO user_sessions (token, expires_at)
            VALUES (?, datetime('now', ?))
            """,
            (token, f"+{hours} hours"),
        )
        conn.commit()


def validate_user_session(token: str) -> bool:
    """Return True if session token exists in SQLite and has not expired."""
    if not token:
        return False
    try:
        with _db() as (_, cur):
            cur.execute(
                "SELECT token FROM user_sessions WHERE token = ? AND expires_at > datetime('now')",
                (token,),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def delete_user_session(token: str) -> None:
    """Delete a session token when user logs out or session is invalidated."""
    if not token:
        return
    try:
        with _db() as (conn, cur):
            cur.execute("DELETE FROM user_sessions WHERE token = ?", (token,))
            conn.commit()
    except Exception:
        pass


# ── AUTO-INIT ─────────────────────────────────────────────────────────────────
init_db()
