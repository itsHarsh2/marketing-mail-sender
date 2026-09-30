"""
send_worker.py — background dispatcher for scheduled sends.

Streamlit only runs your script when a browser interacts with it, so a
scheduled send needs something outside that cycle. This module runs one
daemon thread inside the Streamlit process that polls the scheduled_sends
table and dispatches jobs whose time has come.

IMPORTANT LIMITATION
    The thread lives inside the app process. If the app is closed or the
    machine sleeps, nothing is sent until the app is opened again — at which
    point overdue jobs fire immediately (they are still "due"). This is not
    a substitute for a real scheduler such as cron or systemd; for unattended
    sending, run this app as a long-lived service.

Sender credentials are never stored in a schedule. The worker loads saved
sender accounts from the database and decrypts them at send time, so
scheduling requires accounts saved via Manage Senders.
"""

import threading
from datetime import datetime

from database import (
    get_due_scheduled_sends,
    claim_scheduled_send,
    finish_scheduled_send,
    load_sender_accounts,
)
from utils import log_info, log_error, log_warning

POLL_SECONDS = 30

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_lock = threading.Lock()


def _dispatch(job: dict) -> None:
    """Run one scheduled job. Assumes the job has already been claimed."""
    # Imported here to avoid a circular import at module load time.
    from followup_scheduler import send_followups

    job_id  = job["id"]
    records = job.get("payload") or []
    label   = job.get("label") or f"job #{job_id}"

    if not records:
        finish_scheduled_send(job_id, "error", "Nothing to send — empty payload.")
        return

    senders = [s for s in load_sender_accounts()
               if s.get("email") and s.get("password")]
    if not senders:
        msg = ("No usable saved sender accounts. Save your accounts in "
               "Manage Senders so scheduled sends can authenticate.")
        log_error(f"Scheduled {label}: {msg}")
        finish_scheduled_send(job_id, "error", msg)
        return

    log_info(f"⏰ Running scheduled {label} — {len(records)} email(s).")
    try:
        summary = send_followups(records, senders)
        finish_scheduled_send(
            job_id, "sent",
            f"sent {summary['sent']}, failed {summary['failed']}, "
            f"skipped {summary['skipped']}",
        )
        log_info(
            f"⏰ Scheduled {label} complete — sent {summary['sent']}, "
            f"failed {summary['failed']}."
        )
    except Exception as exc:
        log_error(f"Scheduled {label} failed: {exc}")
        finish_scheduled_send(job_id, "error", str(exc))


def run_due_jobs() -> int:
    """
    Dispatch every job that is due now. Returns how many ran.

    Safe to call directly — the UI uses it to run something immediately
    without waiting for the next poll.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        due = get_due_scheduled_sends(now)
    except Exception as exc:
        log_error(f"Could not read scheduled sends: {exc}")
        return 0

    ran = 0
    for job in due:
        # Only the caller that claims the job sends it, so a second tab or a
        # second worker cannot double-send the same batch.
        if not claim_scheduled_send(job["id"]):
            continue
        _dispatch(job)
        ran += 1
    return ran


def _loop() -> None:
    log_info("📬 Scheduled-send worker started.")
    while not _stop_event.wait(POLL_SECONDS):
        try:
            run_due_jobs()
        except Exception as exc:
            log_error(f"Scheduled-send worker error: {exc}")
    log_info("📬 Scheduled-send worker stopped.")


def start_worker() -> None:
    """Start the poller once per process. Safe to call on every rerun."""
    global _worker_thread
    with _lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _stop_event.clear()
        _worker_thread = threading.Thread(
            target=_loop, name="scheduled-send-worker", daemon=True
        )
        _worker_thread.start()


def stop_worker() -> None:
    """Ask the poller to stop after its current sleep."""
    _stop_event.set()


def worker_alive() -> bool:
    return _worker_thread is not None and _worker_thread.is_alive()
