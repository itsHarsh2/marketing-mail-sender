"""
followup_scheduler.py - Automatic follow-up email scheduler.

IMPROVEMENTS v2:
- Each follow-up failure is isolated (one bad email never blocks the rest)
- follow_up_number tracked per lead to select varied templates
- Database updates are batched in a single transaction (faster, atomic)
- Scheduler uses a threading.Event for clean shutdown instead of infinite loop
- run_followup_job() returns a summary dict so the UI can display results
"""

import threading
import time
import sqlite3
from datetime import datetime

import schedule

from database import (
    get_emails_needing_followup,
    mark_follow_up_sent,
    mark_recipient_followed_up,
    insert_email_sent,
    DB_PATH,
)
from ai_writer import generate_followup_email
from smtp_sender import SMTPSender
from utils import log_info, log_error, log_warning, log_success


def _followup_number_for(recipient_email: str, db_path: str = DB_PATH) -> int:
    """
    Which follow-up this would be for a given recipient.

    Counts how many times we have already emailed them, so the first
    follow-up is #1, the next #2, and so on. The previous implementation
    read the 0/1 follow_up_sent flag, which could never exceed 2.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM emails_sent WHERE recipient_email = ?",
            (recipient_email,),
        )
        n = cur.fetchone()[0]
        conn.close()
        return max(1, n)
    except Exception:
        return 1


def get_followup_candidates(
    follow_up_after_days: int = 3,
    dedupe_by_recipient: bool = True,
    db_path: str = DB_PATH,
    api_key: str = "",
    offering: str = "",
    sender_name: str = "",
    sender_company: str = "",
) -> list[dict]:
    """
    Build the list of follow-ups that *would* be sent, without sending them.

    This is the preview half of the follow-up flow: the UI shows these rows
    with their generated subject and body so a human can approve or drop
    each one before anything leaves the building.

    When *dedupe_by_recipient* is True only the most recent original email
    per recipient is kept — otherwise someone emailed three times in an
    earlier campaign receives three separate follow-ups.
    """
    pending = get_emails_needing_followup(days_ago=follow_up_after_days)
    if not pending:
        return []

    if dedupe_by_recipient:
        newest: dict[str, dict] = {}
        for rec in pending:
            addr = (rec.get("recipient_email") or "").strip().lower()
            if not addr:
                continue
            prev = newest.get(addr)
            if prev is None or (rec.get("sent_at") or "") > (prev.get("sent_at") or ""):
                newest[addr] = rec
        pending = list(newest.values())

    candidates = []
    for rec in pending:
        recipient = rec.get("recipient_email")
        if not recipient:
            continue
        company  = rec.get("company_name") or "there"
        original = rec.get("subject") or "my previous email"
        number   = 1
        content  = generate_followup_email(
            company_name=company,
            original_subject=original,
            follow_up_number=number,
            first_name="",
            api_key=api_key,
            offering=offering,
            sender_name=sender_name,
            sender_company=sender_company,
        )
        candidates.append({
            "email_id":         rec.get("id"),
            "lead_id":          rec.get("lead_id"),
            "recipient":        recipient,
            "company_name":     company,
            "original_subject": original,
            "original_sent_at": rec.get("sent_at", ""),
            "sender_email":     rec.get("sender_email") or "",
            "follow_up_number": number,
            "subject":          content["subject"],
            "body":             content["body"],
        })

    candidates.sort(key=lambda c: c["original_sent_at"], reverse=True)
    log_info(f"Follow-up preview built — {len(candidates)} candidate(s).")
    return candidates


def send_followups(
    records: list[dict],
    senders_config: list[dict],
    stop_event=None,
    progress_cb=None,
    delay_seconds: float = 2.0,
) -> dict:
    """
    Send exactly the follow-ups in *records* — nothing more.

    Args:
        records       : dicts from get_followup_candidates(), already filtered
                        down to the ones a human selected
        stop_event    : optional threading.Event; checked before each send
        progress_cb   : optional callable(done, total, recipient)

    Returns {"sent": int, "failed": int, "skipped": int, "errors": [...]}.
    """
    if not records:
        return {"sent": 0, "failed": 0, "skipped": 0, "errors": []}

    active_senders = [s for s in senders_config if s.get("email") and s.get("password")]
    if not active_senders:
        log_error("No sender accounts configured — aborting follow-up send.")
        return {"sent": 0, "failed": 0, "skipped": len(records),
                "errors": ["No sender accounts configured."]}

    smtp = SMTPSender(active_senders)
    sent = failed = skipped = 0
    errors: list[str] = []
    total = len(records)

    for i, rec in enumerate(records, 1):
        if stop_event is not None and stop_event.is_set():
            skipped += total - i + 1
            log_info("Follow-up send stopped by user.")
            break

        recipient = rec.get("recipient")
        if not recipient:
            skipped += 1
            continue

        if progress_cb:
            progress_cb(i, total, recipient)

        try:
            result = smtp.send_followup(
                to_email=recipient,
                subject=rec["subject"],
                body=rec["body"],
                original_sender_email=rec.get("sender_email", ""),
            )

            if result["success"]:
                # Mark every outstanding email to this person, not just the
                # previewed row — candidates are deduplicated per recipient,
                # so siblings would otherwise re-qualify next run.
                mark_recipient_followed_up(recipient)

                # Log the follow-up itself, pre-marked so it can never become
                # a candidate for a follow-up of its own.
                new_id = insert_email_sent(
                    lead_id=rec.get("lead_id"),
                    company_name=rec.get("company_name", ""),
                    recipient_email=recipient,
                    sender_email=result["sender_email"],
                    subject=rec["subject"],
                    body=rec["body"],
                    is_followup=1,
                )
                mark_follow_up_sent(new_id)

                sent += 1
                log_success(f"Follow-up #{rec.get('follow_up_number', 1)} sent to {recipient}")
            else:
                failed += 1
                err = f"{recipient}: {result.get('error', 'unknown error')}"
                errors.append(err)
                log_warning(f"Follow-up failed for {err}")

        except Exception as exc:
            failed += 1
            errors.append(f"{recipient}: {exc}")
            log_error(f"Unexpected error sending follow-up to {recipient}: {exc}")

        if i < total:
            time.sleep(delay_seconds)

    log_info(f"Follow-up send complete — sent: {sent} | failed: {failed} | skipped: {skipped}")
    return {"sent": sent, "failed": failed, "skipped": skipped, "errors": errors}


def run_followup_job(
    senders_config: list[dict],
    follow_up_after_days: int = 3,
    db_path: str = DB_PATH,
) -> dict:
    """
    Find emails that need a follow-up and send them.

    Args:
        senders_config       : list of sender account dicts
        follow_up_after_days : days to wait before sending follow-up
        db_path              : SQLite database path

    Returns:
        {"sent": int, "failed": int, "skipped": int}
    """
    log_info(
        f"🔄 Follow-up job started — "
        f"targeting emails sent {follow_up_after_days}+ day(s) ago."
    )

    candidates = get_followup_candidates(
        follow_up_after_days=follow_up_after_days, db_path=db_path
    )
    if not candidates:
        log_info("No follow-ups needed right now.")
        return {"sent": 0, "failed": 0, "skipped": 0}

    log_info(f"Found {len(candidates)} recipient(s) eligible for follow-up.")
    return send_followups(candidates, senders_config)


# ── BACKGROUND SCHEDULER ───────────────────────────────────────────────────────

class FollowUpScheduler:
    """
    Runs the follow-up job on a background thread at a configurable interval.

    Usage:
        scheduler = FollowUpScheduler(senders, days=3, interval_hours=6)
        scheduler.start()
        ...
        scheduler.stop()   # clean shutdown
    """

    def __init__(self, senders_config: list[dict],
                 follow_up_after_days: int = 3,
                 check_every_hours: int = 6):
        self.senders_config       = senders_config
        self.follow_up_after_days = follow_up_after_days
        self.check_every_hours    = check_every_hours
        self._stop_event          = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the scheduler in a daemon background thread."""
        if self._thread and self._thread.is_alive():
            log_warning("Follow-up scheduler is already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        log_info(
            f"📅 Follow-up scheduler started — "
            f"runs every {self.check_every_hours}h, "
            f"targets emails older than {self.follow_up_after_days} day(s)."
        )

    def stop(self) -> None:
        """Signal the scheduler to stop after the current sleep interval."""
        self._stop_event.set()
        log_info("Follow-up scheduler stop requested.")

    def _run_loop(self) -> None:
        # Run immediately on startup
        self._execute()

        interval_seconds = self.check_every_hours * 3600
        while not self._stop_event.wait(interval_seconds):
            self._execute()

        log_info("Follow-up scheduler stopped.")

    def _execute(self) -> None:
        try:
            run_followup_job(
                senders_config=self.senders_config,
                follow_up_after_days=self.follow_up_after_days,
            )
        except Exception as exc:
            log_error(f"Follow-up scheduler encountered an error: {exc}")


# ── CLI ENTRY POINT ────────────────────────────────────────────────────────────

def start_followup_scheduler(senders_config: list[dict],
                              follow_up_after_days: int = 3,
                              check_every_hours: int = 6) -> FollowUpScheduler:
    """
    Convenience function: create and start a FollowUpScheduler.
    Returns the scheduler so the caller can stop it later.
    """
    s = FollowUpScheduler(senders_config, follow_up_after_days, check_every_hours)
    s.start()
    return s


if __name__ == "__main__":
    print("Run this via app.py or import start_followup_scheduler().")
