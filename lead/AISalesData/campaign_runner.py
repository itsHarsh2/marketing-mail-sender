"""
campaign_runner.py — runs campaigns on a background thread.

WHY THIS EXISTS
    Streamlit terminates an in-flight script run whenever a rerun is
    triggered, by raising StopException at the script's next Streamlit API
    call. A campaign that ran inside the script run was therefore killed by
    any incidental browser event — a click, a tab refocus, a websocket
    reconnect. A six-minute scrape could be destroyed silently, losing every
    lead, because the very next line after scraping touched st.session_state.

    Doing the work on a daemon thread fixes that: the pipeline no longer
    depends on the browser staying still, the UI stays responsive, progress
    updates live, and STOP actually stops something.

THE RULE FOR THIS MODULE
    Nothing here may touch `st.` anything. A background thread has no script
    context, so Streamlit calls from it either fail or are ignored. All state
    is kept in the module-level _STATE object behind a lock, and the UI polls
    snapshot() for a plain dict.
"""

import threading
import time
from datetime import datetime

from database import (
    insert_lead, insert_email_sent, create_campaign, update_campaign_status,
    deduplicate_leads, has_initial_email_sent, save_paused_campaign,
    get_paused_campaign, get_paused_campaigns,
)
from maps_scraper import scrape_google_maps
from email_finder import batch_find_emails
from email_verifier import filter_leads_by_valid_email
from ai_writer import generate_cold_email
from smtp_sender import SMTPSender
from utils import log_info, log_error, log_warning, clear_log_buffer


# ── STATE ──────────────────────────────────────────────────────────────────────
# status transitions:
#   idle → running → awaiting_review → sending → done
#                 ↘ error / cancelled / paused ↙

class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self._reset_fields()

    def _reset_fields(self):
        self.status      = "idle"
        self.step        = ""
        self.detail      = ""
        self.leads_found = 0
        self.done        = 0
        self.total       = 0
        self.sent        = 0
        self.error       = None
        self.prepared    = []
        self.campaign_id = None
        self.source      = ""
        self.started_at  = None
        self.stop_event  = threading.Event()

    def reset(self):
        with self.lock:
            self._reset_fields()

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot_stop_event(self) -> threading.Event:
        """The live Event, so a worker sees a stop requested after it started."""
        with self.lock:
            return self.stop_event

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status, "step": self.step, "detail": self.detail,
                "leads_found": self.leads_found, "done": self.done,
                "total": self.total, "sent": self.sent, "error": self.error,
                "prepared": list(self.prepared), "campaign_id": self.campaign_id,
                "source": self.source, "started_at": self.started_at,
                "stopping": self.stop_event.is_set(),
            }


_STATE = _State()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def snapshot() -> dict:
    return _STATE.snapshot()


def is_busy() -> bool:
    return _STATE.snapshot()["status"] in ("running", "sending")


def request_stop() -> None:
    """Ask the running pipeline to pause at its next checkpoint."""
    with _STATE.lock:
        _STATE.stop_event.set()
    log_info("Pause requested — finishing current item then pausing campaign.")


def reset() -> None:
    """Clear state back to idle (after the user dismisses a result)."""
    _STATE.reset()


def _fail(campaign_id, message):
    log_error(f"Campaign error: {message}")
    if campaign_id:
        try:
            update_campaign_status(campaign_id, "error")
        except Exception:
            pass
    _STATE.update(status="error", error=message)


def _pause_state(campaign_id, prepared=None, sent=0, total=0, detail="Campaign paused.", persist: bool = False):
    prepared = prepared or []
    if campaign_id and persist:
        try:
            save_paused_campaign(campaign_id, prepared, sent_count=sent, total_count=total)
        except Exception:
            pass
    _STATE.update(
        status="paused", prepared=prepared, sent=sent, total=total,
        step=f"Campaign Paused ({sent} sent, {len(prepared)} remaining)",
        detail=detail,
    )
    log_info(f"Campaign #{campaign_id} paused.")


def _safe_params(params):
    """Persist campaign settings only; credentials must never enter SQLite."""
    return {k: v for k, v in params.items() if k not in ("senders", "contacts")}


def _checkpoint(campaign_id, stage, records, params, **extra):
    """Persist enough state to resume this exact pipeline stage after restart."""
    payload = {"params": _safe_params(params), "records": records, **extra}
    save_paused_campaign(campaign_id, records, 0, len(records), stage, payload)


# ── COMPOSE ────────────────────────────────────────────────────────────────────

def _prepare_emails(contacts, senders, ai_api_key, offering, company_name,
                    custom_subject, custom_body, stop_event):
    """Compose one email per contact. Sends nothing."""
    active = [s for s in senders if s.get("email") and s.get("password")]
    sender_name = active[0].get("name", "") if active else ""

    prepared = []
    total = len(contacts)
    _STATE.update(total=total, done=0)

    for i, contact in enumerate(contacts, 1):
        if stop_event.is_set():
            log_info("Composition paused by user.")
            break

        recipient = contact.get("email")
        if not recipient:
            continue

        first_name   = contact.get("first_name", "")
        last_name    = contact.get("last_name", "")
        contact_name = contact.get("company_name") or first_name or "there"

        _STATE.update(done=i, detail=f"Composing for {recipient}")

        content = generate_cold_email(
            api_key=ai_api_key, company_name=contact_name, offering=offering,
            sender_name=sender_name, sender_company=company_name,
            custom_subject=custom_subject, custom_body=custom_body,
            first_name=first_name, last_name=last_name,
        )
        if not content:
            continue

        prepared.append({
            "id":           f"{i}:{recipient}",
            "recipient":    recipient,
            "display_name": first_name or contact_name,
            "company_name": contact_name,
            "db_id":        contact.get("db_id"),
            "subject":      content["subject"],
            "body":         content["body"],
        })

    return prepared


def _deduplicate_scraped_businesses(leads):
    """Remove repeated Maps listings before website and email lookups."""
    unique = []
    seen = set()
    for lead in leads:
        company = (lead.get("company_name") or "").strip().lower()
        website = (lead.get("website") or "").strip().lower().rstrip("/")
        # A website is the reliable identity before an email is found.  Keep
        # entries without one; address-level de-duplication happens later.
        if not website:
            unique.append(lead)
            continue
        key = (company, website)
        if key not in seen:
            seen.add(key)
            unique.append(lead)
    return unique


# ── PIPELINE ───────────────────────────────────────────────────────────────────

def _pipeline(params):
    """Gather contacts and compose emails, then wait for human approval."""
    campaign_id = None
    stop_event  = _STATE.snapshot_stop_event()
    try:
        clear_log_buffer()
        source = params["source"]

        resuming = bool(params.get("campaign_id"))
        campaign_id = params.get("campaign_id") or create_campaign(
            params.get("business_type", "list_import"), params.get("location", "uploaded_list"),
            params.get("offering", ""), params.get("company_name", ""), params.get("company_website", ""))
        _STATE.update(campaign_id=campaign_id, source=source)
        log_info(f"Campaign #{campaign_id} {'resumed' if resuming else 'started'} ({source}).")

        # ── Gather contacts ────────────────────────────────────────────────
        if source == "scraper":
            _STATE.update(step="Step 1/4 — Scraping Google Maps…")
            log_info("Step 1/4 — Scraping Google Maps...")
            leads = scrape_google_maps(
                business_type=params["business_type"],
                location=params["location"],
                max_scrolls=params["max_scrolls"],
                headless=True,
                stop_event=stop_event,
            )
            # A Maps pause always resumes Maps first.  Even when a few
            # listings were already extracted, do not start email collection
            # or composition from that partial result set.
            if stop_event.is_set():
                # Preserve listings collected before an earlier Maps pause as
                # well as those collected in this resumed browser session.
                if params.get("saved_scraped_leads"):
                    leads = params["saved_scraped_leads"] + leads
                _checkpoint(campaign_id, "scraping", leads, params)
                return _pause_state(
                    campaign_id, leads, 0, len(leads),
                    "Paused during Google Maps scraping. Resume will finish scraping before finding or sending any emails.",
                )

            # A resumed Maps pass can rediscover earlier listings.  Merge
            # them with the checkpoint before moving to email collection.
            previous = params.get("saved_scraped_leads", [])
            if previous:
                seen, merged = set(), []
                for lead in previous + leads:
                    key = ((lead.get("company_name") or "").strip().lower(),
                           (lead.get("website") or "").strip().lower())
                    if key not in seen:
                        seen.add(key)
                        merged.append(lead)
                leads = merged
            if not leads:
                return _fail(campaign_id,
                             "No businesses found. Try different terms or a "
                             "broader location.")
            before_lookup = len(leads)
            leads = _deduplicate_scraped_businesses(leads)
            if len(leads) != before_lookup:
                log_info(
                    f"Removed {before_lookup - len(leads)} duplicate Maps listing(s) "
                    "before email lookup."
                )

            _STATE.update(leads_found=len(leads))
            log_info(f"Step 1 complete — {len(leads)} businesses found.")

            _STATE.update(step=f"Step 2/4 — Finding emails on {len(leads)} websites…")
            log_info("Step 2/4 — Finding email addresses...")
            _checkpoint(campaign_id, "finding_emails", leads, params)
            leads = batch_find_emails(
                leads, max_leads=len(leads), stop_event=stop_event,
                progress_callback=lambda _i, current: _checkpoint(campaign_id, "finding_emails", current, params),
            )

            # Different listings can resolve to one shared email address.
            # Filter that address before saving leads or composing messages.
            skip_already_sent = params.get("skip_already_sent", True)
            leads, dedup_stats = deduplicate_leads(
                leads, skip_already_sent=skip_already_sent
            )
            log_info(
                f"Post-lookup de-duplication — {dedup_stats['kept']}/{dedup_stats['total']} "
                f"lead(s) kept ({dedup_stats['batch_duplicates']} duplicate address(es), "
                f"{dedup_stats['past_sent_duplicates']} previously contacted)."
            )

            # Insert all scraped leads into SQLite database table `leads` so zero scraped leads are lost
            for lead in leads:
                if lead.get("company_name"):
                    lead["db_id"] = insert_lead(
                        company_name=lead["company_name"],
                        website=lead.get("website"), email=lead.get("email"),
                        location=params.get("location", ""),
                        business_type=params.get("business_type", "scraper"),
                    )

            contacts = [
                {"email": l.get("email"), "first_name": "", "last_name": "",
                 "company_name": l["company_name"], "website": l.get("website"), "db_id": l.get("db_id")}
                for l in leads if l.get("company_name")
            ]

            if stop_event.is_set():
                _checkpoint(campaign_id, "composing", contacts, params)
                return _pause_state(
                    campaign_id, contacts, 0, len(contacts),
                    f"Paused by user — {len(contacts)} business(es) saved in database. Click 'Resume Campaign' to compose and send."
                )
        elif source in ("list", "paused_resume"):
            contacts = params["contacts"]
            needs_email_search = [c for c in contacts if not c.get("email") and c.get("website")]
            if needs_email_search:
                _STATE.update(step=f"Finding emails for {len(needs_email_search)} saved lead(s)…")
                log_info(f"Finding emails for {len(needs_email_search)} saved lead(s)...")
                _checkpoint(campaign_id, "finding_emails", contacts, params)
                batch_find_emails(
                    needs_email_search, max_leads=len(needs_email_search), stop_event=stop_event,
                    progress_callback=lambda _i, _current: _checkpoint(campaign_id, "finding_emails", contacts, params),
                )

            skip_already_sent = params.get("skip_already_sent", True)
            contacts, dedup_stats = deduplicate_leads(contacts, skip_already_sent=skip_already_sent)
            log_info(
                f"Deduplication — {dedup_stats['kept']}/{dedup_stats['total']} contact(s) kept "
                f"({dedup_stats['batch_duplicates']} duplicate(s), "
                f"{dedup_stats['past_sent_duplicates']} previously contacted)."
            )
            if not contacts:
                return _fail(campaign_id,
                             f"All {dedup_stats['total']} contact(s) in this list were already sent an email in a previous campaign.")

            _STATE.update(leads_found=len(contacts))
            for c in contacts:
                c["db_id"] = insert_lead(
                    company_name=c.get("company_name", ""), website=c.get("website"),
                    email=c.get("email"), location="", business_type="list_import",
                )

        else:
            contacts = params["contacts"]

        if stop_event.is_set() and not contacts:
            return _pause_state(campaign_id, [], 0, 0, "Paused by user.")

        # ── Compose (no sending) ───────────────────────────────────────────
        _STATE.update(step="Step 4/4 — Composing emails for your review…")
        log_info("Step 4/4 — Composing emails (nothing is sent yet)...")
        prepared = _prepare_emails(
            contacts, params["senders"], params["ai_api_key"],
            params["offering"], params["company_name"],
            params["custom_subject"], params["custom_body"], stop_event,
        )

        if stop_event.is_set():
            if prepared:
                save_paused_campaign(campaign_id, prepared, 0, len(prepared))
                _STATE.update(
                    status="awaiting_review", prepared=prepared,
                    step=f"{len(prepared)} email(s) ready for your review (Paused)",
                    detail="Campaign paused during composition.",
                )
                return
            else:
                save_paused_campaign(campaign_id, contacts, 0, len(contacts))
                return _pause_state(campaign_id, contacts, 0, len(contacts), "Paused by user during composition. Leads saved.")

        if not prepared:
            return _fail(campaign_id, "No emails could be composed.")

        _STATE.update(
            status="awaiting_review", prepared=prepared,
            step=f"{len(prepared)} email(s) ready for your review",
            detail="",
        )
        log_info(f"{len(prepared)} email(s) composed and awaiting review — "
                 "nothing has been sent.")

    except Exception as exc:
        _fail(campaign_id, str(exc))


def _cancel(campaign_id):
    if campaign_id:
        try:
            update_campaign_status(campaign_id, "cancelled")
        except Exception:
            pass
    _STATE.update(status="cancelled", step="Cancelled", detail="")
    log_info("Campaign cancelled.")


# ── SEND ───────────────────────────────────────────────────────────────────────

def _send(records, senders, campaign_id):
    """Send exactly the approved emails, on the background thread."""
    stop_event = _STATE.snapshot_stop_event()
    try:
        active = [s for s in senders if s.get("email") and s.get("password")]
        smtp   = SMTPSender(active)
        total  = len(records)
        sent   = 0
        skipped_existing = 0
        _STATE.update(total=total, done=0, sent=0,
                      step=f"Sending {total} approved email(s)…")

        for i, item in enumerate(records, 1):
            if stop_event.is_set():
                remaining = records[i - 1:]
                log_info(f"Sending paused by user — {sent} sent, {len(remaining)} remaining.")
                if campaign_id:
                    save_paused_campaign(campaign_id, remaining, sent_count=sent, total_count=total)
                _STATE.update(
                    status="paused", prepared=remaining, sent=sent, total=total,
                    step=f"Paused — {sent} of {total} sent ({len(remaining)} remaining)",
                    detail="Campaign paused. Click 'Resume Sending' to continue.",
                )
                return

            _STATE.update(done=i, detail=f"Sending to {item['recipient']}")
            if has_initial_email_sent(item["recipient"]):
                skipped_existing += 1
                log_warning(
                    f"[{i}/{total}] SKIP {item['recipient']} — an initial campaign "
                    "email is already recorded for this recipient."
                )
                continue

            result = smtp.send_email(
                to_email=item["recipient"], subject=item["subject"],
                body=item["body"], company_name=item.get("company_name", ""),
            )
            if result["success"]:
                insert_email_sent(
                    lead_id=item.get("db_id"),
                    company_name=item.get("company_name", ""),
                    recipient_email=item["recipient"],
                    sender_email=result["sender_email"],
                    subject=item["subject"], body=item["body"],
                    campaign_id=campaign_id,
                    message_id=result.get("message_id"),
                )
                sent += 1
                _STATE.update(sent=sent)

            log_info(f"[{i}/{total}] {'OK' if result['success'] else 'FAIL'} "
                     f"{item['display_name'][:40]} <{item['recipient']}>")
            time.sleep(1.5)

        if campaign_id:
            try:
                update_campaign_status(campaign_id, "complete")
            except Exception:
                pass

        summary = f"Sent {sent} of {total}"
        if skipped_existing:
            summary += f" ({skipped_existing} duplicate recipient(s) skipped)"
        _STATE.update(status="done", sent=sent, prepared=[], step=summary, detail="")
        log_info(f"Campaign complete — {summary}.")

    except Exception as exc:
        _fail(campaign_id, str(exc))


# ── PUBLIC API ─────────────────────────────────────────────────────────────────

def _spawn(target, args):
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return False
        _STATE.update(started_at=datetime.now().strftime("%H:%M:%S"))
        _thread = threading.Thread(target=target, args=args, daemon=True,
                                   name="campaign-runner")
        _thread.start()
        return True


def start_campaign(params) -> bool:
    """Begin gathering + composing on a background thread."""
    if is_busy():
        return False
    _STATE.reset()
    _STATE.update(status="running", step="Starting…", source=params["source"])
    return _spawn(_pipeline, (params,))


def start_send(records, senders, campaign_id) -> bool:
    """Send approved emails on a background thread."""
    if is_busy():
        return False
    with _STATE.lock:
        _STATE.stop_event = threading.Event()
        _STATE.status = "sending"
        _STATE.prepared = list(records)
        _STATE.campaign_id = campaign_id
    return _spawn(_send, (records, senders, campaign_id))


def resume_send(records, senders, campaign_id) -> bool:
    """Resume sending remaining unsent emails on a background thread."""
    if is_busy():
        return False
    with _STATE.lock:
        _STATE.stop_event = threading.Event()
        _STATE.status = "sending"
        _STATE.prepared = list(records)
        _STATE.campaign_id = campaign_id
    return _spawn(_send, (records, senders, campaign_id))


def resume_campaign(campaign_id: int, senders: list[dict], ai_api_key: str = "", offering: str = "", company_name: str = "") -> bool:
    """Resume a paused campaign, whether paused during scraping/composition or sending."""
    if is_busy():
        return False
    pc = get_paused_campaign(campaign_id)
    records = []
    if pc:
        records = pc.get("unsent_records", [])
    else:
        snap = snapshot()
        if snap["status"] == "paused" and snap.get("prepared"):
            records = snap["prepared"]
            campaign_id = campaign_id or snap.get("campaign_id")

    if not records:
        # A Maps pause can happen before the first listing is extracted.
        if not pc:
            return False

    # Case A: Emails were already composed (items have 'subject' and 'body')
    if records and "subject" in records[0] and "body" in records[0]:
        return resume_send(records, senders, campaign_id)

    # Case B: Paused during scraping/finding before emails were composed
    saved = (pc or {}).get("resume_data", {})
    params = {
        "source": "paused_resume",
        "contacts": records,
        "senders": senders,
        "ai_api_key": ai_api_key,
        "offering": offering or (pc.get("offering") if pc else ""),
        "company_name": company_name or (pc.get("company_name") if pc else ""),
        "company_website": (pc.get("company_website") if pc else ""),
        "business_type": (pc.get("business_type") if pc else ""),
        "location": (pc.get("location") if pc else ""),
        "max_scrolls": 15,
        "custom_subject": "",
        "custom_body": "",
        "skip_already_sent": True,
    }
    params.update(saved.get("params", {}))
    params["campaign_id"] = campaign_id
    # Restart only the incomplete Maps stage; all later stages use the
    # checkpointed leads, including emails already found.
    if (pc or {}).get("resume_stage") in ("finding_emails", "composing"):
        params["source"] = "paused_resume"
    elif not records:
        # Compatibility with pauses created before durable stage metadata was
        # added: reopen Maps but keep the original campaign record.
        params["source"] = "scraper"
    elif (pc or {}).get("resume_stage") == "scraping":
        params["source"] = "scraper"
        params["saved_scraped_leads"] = records
    _STATE.reset()
    _STATE.update(status="running", step="Resuming paused campaign…", campaign_id=campaign_id)
    return _spawn(_pipeline, (params,))
