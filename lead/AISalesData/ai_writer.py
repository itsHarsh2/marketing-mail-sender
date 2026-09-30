"""
ai_writer.py  —  Email generation: AI, manual template, or fallback.

v4 CHANGES:
- generate_cold_email() now accepts first_name + last_name parameters.
  When available they are used in the AI prompt and in fallback templates
  so every email opens with "Hi John," instead of "Hi Acme Corp team,".
- apply_manual_template() replaces {first_name}, {last_name}, {full_name}
  AND {company_name} placeholders.
- All existing call sites still work because new params default to "".
"""

import json
import random
import re
import time

import requests
from openai import OpenAI, RateLimitError, AuthenticationError, APIError
from utils import log_info, log_error, log_warning

# ── CLIENT CACHE ───────────────────────────────────────────────────────────────

_clients = {}

def _get_client(api_key):
    if api_key not in _clients:
        _clients[api_key] = OpenAI(api_key=api_key)
    return _clients[api_key]

def _sanitise(text):
    return re.sub(r"[{}`]", "", (text or "")).strip()[:300]


def is_gemini_key(api_key: str) -> bool:
    """Detect if an API key is a Google Gemini key (any non-sk key)."""
    key = (api_key or "").strip()
    return bool(key and len(key) >= 10 and not key.startswith("sk-"))


# ── AI PROMPT ─────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a concise, friendly sales copywriter. "
    "You write short cold emails that feel personal, not spammy. "
    "You always respond with valid JSON and nothing else."
)

def _build_prompt(first_name, company_name, offering, sender_name, sender_company):
    greeting_name = first_name if first_name else company_name

    return f"""Write a cold outreach email from {sender_name} at {sender_company} to {greeting_name}.
Context: {greeting_name} works at / is {company_name}.
The email is about: {offering}

Rules:
- Under 80 words total (body only, not counting subject)
- Open with "Hi {greeting_name}," — use the first name, not a generic greeting
- Friendly and conversational, not corporate
- No clichés like "I hope this email finds you well"
- End with: "Would you be open to a quick 10-minute call?"
- No signature (added separately)

Respond ONLY with this JSON:
{{
  "subject": "<subject line, 6-10 words>",
  "body": "<email body, under 80 words>"
}}"""


def _call_gemini_api(api_key: str, prompt: str, system_prompt: str = None):
    """Call Google Gemini REST API."""
    api_key = api_key.strip()
    # Official supported Google Gemini model endpoints
    models_to_try = ["gemini-3.5-flash-lite", "gemini-3.5-flash-lite"]

    full_prompt = prompt
    if system_prompt:
        full_prompt = f"{system_prompt}\n\n{prompt}"

    payload = {
        "contents": [{
            "parts": [{"text": full_prompt}]
        }],
        "generationConfig": {
            "temperature": 0.7,
            "responseMimeType": "application/json"
        }
    }

    last_err = "Unknown Gemini error"
    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=25)
            if res.status_code == 200:
                data = res.json()
                try:
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    return text.strip(), None
                except Exception as exc:
                    return None, f"Failed to extract text from Gemini response: {exc}"
            else:
                try:
                    err_msg = res.json().get("error", {}).get("message", res.text)
                except Exception:
                    err_msg = res.text
                last_err = f"Google Gemini API error ({res.status_code}): {err_msg}"
        except Exception as exc:
            last_err = f"Google Gemini connection error: {exc}"

    return None, last_err


def _call_api_with_backoff(client, prompt, max_tokens=350, system=None):
    models_to_try = ["gpt-4o-mini", "gpt-3.5-turbo"]
    last_err = "Unknown error"
    for model_name in models_to_try:
        for attempt in range(1, 3):
            try:
                kwargs = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system or _SYSTEM_PROMPT},
                        {"role": "user",   "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.85,
                }
                try:
                    resp = client.chat.completions.create(
                        **kwargs, response_format={"type": "json_object"}
                    )
                except Exception:
                    resp = client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content.strip(), None
            except RateLimitError as exc:
                wait = 2 ** attempt + random.uniform(0, 1)
                log_warning(f"OpenAI rate limit ({model_name} attempt {attempt}/2). Waiting {wait:.1f}s...")
                last_err = f"Rate limit or quota exceeded: {exc}"
                time.sleep(wait)
            except AuthenticationError as exc:
                log_error(f"OpenAI API key invalid: {exc}")
                return None, f"Invalid OpenAI API key: {exc}"
            except Exception as exc:
                log_error(f"OpenAI API error with {model_name} (attempt {attempt}/2): {exc}")
                last_err = str(exc)
                if attempt < 2:
                    time.sleep(1.5)
    return None, last_err


def _parse_json_response(raw):
    if not raw:
        return None, None
    try:
        clean_raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
        clean_raw = re.sub(r"\s*```$", "", clean_raw).strip()
        data = json.loads(clean_raw)
        subject = str(data.get("subject", "")).strip()
        body    = str(data.get("body", "")).strip()
        if subject and body:
            return subject, body
    except Exception as exc:
        log_warning(f"Could not parse AI JSON: {exc}")
    return None, None


# ── MANUAL TEMPLATE ────────────────────────────────────────────────────────────

def apply_manual_template(custom_subject, custom_body,
                          company_name, first_name="", last_name=""):
    """
    Replace placeholders in the user's own subject + body.
    """
    full_name = (first_name + " " + last_name).strip() or company_name

    def _replace(text):
        text = re.sub(r"\{first_name\}",   first_name or company_name, text, flags=re.IGNORECASE)
        text = re.sub(r"\{last_name\}",    last_name,                  text, flags=re.IGNORECASE)
        text = re.sub(r"\{full_name\}",    full_name,                  text, flags=re.IGNORECASE)
        text = re.sub(r"\{company_name\}", company_name,               text, flags=re.IGNORECASE)
        text = re.sub(r"\{\{first_name\}\}",   first_name or company_name, text, flags=re.IGNORECASE)
        text = re.sub(r"\{\{last_name\}\}",    last_name,                  text, flags=re.IGNORECASE)
        text = re.sub(r"\{\{full_name\}\}",    full_name,                  text, flags=re.IGNORECASE)
        text = re.sub(r"\{\{company_name\}\}", company_name,               text, flags=re.IGNORECASE)
        return text

    return {
        "subject": _replace(custom_subject.strip()),
        "body":    _replace(custom_body.strip()),
    }


def _smart_ai_call(api_key: str, prompt: str, system_prompt: str = None):
    """
    Universal AI caller: Routes keys cleanly to Google Gemini or OpenAI.
    """
    key = (api_key or "").strip()
    if not key:
        return None, "No AI API key provided."

    if key.startswith("sk-"):
        try:
            client = _get_client(key)
            return _call_api_with_backoff(client, prompt, system=system_prompt)
        except Exception as exc:
            return None, str(exc)
    else:
        # Call Gemini API for non-sk keys (AQ., AS., II., AIzaSy, etc.)
        return _call_gemini_api(key, prompt, system_prompt)


# ── MAIN FUNCTION ──────────────────────────────────────────────────────────────

def generate_cold_email(
    api_key,
    company_name,
    offering,
    sender_name,
    sender_company,
    custom_subject="",
    custom_body="",
    first_name="",
    last_name="",
):
    """
    Generate or compose a cold email.
    """

    # 1. Manual template
    if custom_subject and custom_body:
        log_info(f"  Using manual template for {first_name or company_name}")
        return apply_manual_template(
            custom_subject, custom_body,
            company_name, first_name, last_name
        )

    # 2. AI mode (Universal Gemini / OpenAI caller)
    key = (api_key or "").strip()
    if key:
        cn  = _sanitise(company_name)
        off = _sanitise(offering)
        fn  = _sanitise(first_name)
        sn  = _sanitise(sender_name)
        sc  = _sanitise(sender_company)

        log_info(f"  AI generating email for {fn or cn}...")
        prompt = _build_prompt(fn, cn, off, sn, sc)
        raw, err = _smart_ai_call(key, prompt, _SYSTEM_PROMPT)

        if raw:
            subject, body = _parse_json_response(raw)
            if subject and body:
                log_info(f"  AI email done. Subject: {subject}")
                return {"subject": subject, "body": body}

        log_warning(f"  AI failed for {fn or cn} ({err}), using fallback.")

    # 3. Fallback
    return fallback_email(company_name, offering, sender_name, sender_company,
                          first_name, last_name)


def fallback_email(company_name, offering, sender_name, sender_company,
                   first_name="", last_name=""):
    """Random fallback template — uses first name when available."""
    greeting  = first_name if first_name else f"{company_name} team"
    templates = [
        (
            f"Quick question for {first_name or company_name}",
            f"Hi {greeting},\n\nI came across your details and wanted to reach out. "
            f"We help businesses with {offering}.\n\n"
            f"Would you be open to a quick 10-minute chat?\n\nBest,\n{sender_name}"
        ),
        (
            f"Idea for {first_name or company_name}",
            f"Hi {greeting},\n\nThought our work in {offering} might be a good fit for you.\n\n"
            f"Happy to share ideas — would a short call work?\n\nBest,\n{sender_name}"
        ),
        (
            f"Something that could help {first_name or company_name}",
            f"Hi {greeting},\n\nWe specialise in {offering} and have helped similar "
            f"businesses see solid results.\n\nWorth a quick call?\n\nBest,\n{sender_name}"
        ),
    ]
    subject, body = random.choice(templates)
    return {"subject": subject, "body": body}


# ── AI REVISION ────────────────────────────────────────────────────────────────

_REVISE_SYSTEM_PROMPT = (
    "You revise outbound sales emails. You apply exactly the change the user "
    "asks for, keep the message truthful and free of invented claims, and "
    "never add a signature or sign-off block. "
    "You always respond with valid JSON and nothing else."
)


def revise_email(api_key, subject, body, instruction, recipient_hint=""):
    """
    Rewrite an email according to a free-text instruction.
    Supports ANY API key format (sk-, AQ., AS., II., AIzaSy, etc.).
    """
    key = (api_key or "").strip()
    if not key:
        return None, ("An AI API key is needed to revise with AI. "
                      "Edit the text directly instead, or add a key in the sidebar.")
    if not (instruction or "").strip():
        return None, "Describe the change you want first."

    who = f" The recipient is {recipient_hint}." if recipient_hint else ""
    prompt = f"""Revise the email below.

Requested change: {instruction.strip()}{who}

Current subject: {subject}
Current body:
{body}

Rules:
- Apply the requested change and nothing else
- Keep it a cold outreach email; stay concise
- Do not invent facts, statistics, or claims that are not already present
- Do not add a signature, "Best regards" block, or contact details
- Keep any {{placeholder}} tokens exactly as they appear

Respond ONLY with this JSON:
{{
  "subject": "<revised subject>",
  "body": "<revised body>"
}}"""

    try:
        raw, err = _smart_ai_call(key, prompt, _REVISE_SYSTEM_PROMPT)
        if err:
            return None, err
    except Exception as exc:
        log_error(f"AI revision call failed: {exc}")
        return None, f"AI request failed: {exc}"

    new_subject, new_body = _parse_json_response(raw)
    if new_subject and new_body:
        log_info(f"AI revised email. New subject: {new_subject}")
        return {"subject": new_subject, "body": new_body}, None

    return None, "Could not parse AI response output."


# ── FOLLOW-UP ──────────────────────────────────────────────────────────────────

_FOLLOWUP_TEMPLATES = [
    (
        "Re: {original_subject}",
        "Just following up in case my last note got buried. "
        "I have a couple of quick ideas for driving more traffic to {company_name} I'd love to share, "
        "even if a call isn't ideal right now.\n\n"
        "Worth 10 minutes this week?"
    ),
    (
        "Re: {original_subject}",
        "Following up on my previous message — just wanted to make sure you saw it. "
        "I'd be happy to share a couple of ideas for driving more traffic to {company_name}, "
        "even if a call doesn't make sense right now.\n\n"
        "Worth 10 minutes this week?"
    ),
    (
        "Checking in — {company_name}",
        "One last nudge — if there's any interest in driving more traffic to {company_name}, "
        "I'd love a quick chat. If not, no worries!\n\n"
        "Worth 10 minutes this week?"
    ),
]

def _clean_placeholders(text: str, first_name: str = "", company_name: str = "") -> str:
    """Clean up any unparsed variable brackets or accidental greeting prefixes from output."""
    if not text:
        return ""
    cname = company_name.strip() if company_name else "your company"
    
    # Strip any accidental leading "Hi ...," or "Hello ...," greetings from the start of the body
    text = re.sub(r"^\s*(hi|hello|dear)\s+[^,\n]+,\s*", "", text, flags=re.IGNORECASE)
    # Strip literal variable brackets
    text = re.sub(r"\{first_name\}|\{greeting\}|\{name\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{company_name\}|\{company\}", cname, text, flags=re.IGNORECASE)
    text = re.sub(r"\{[^}]*\}", "", text)
    return text.strip()


def generate_followup_email(company_name, original_subject,
                             follow_up_number=1, first_name="",
                             api_key="", offering="", sender_name="", sender_company=""):
    key = (api_key or "").strip()
    if key:
        cn   = _sanitise(company_name)
        off  = _sanitise(offering)
        fn   = _sanitise(first_name)
        sn   = _sanitise(sender_name)
        sc   = _sanitise(sender_company)
        orig = _sanitise(original_subject)

        cname_str = cn if cn else "your company"
        prompt = (
            f"Generate a concise follow-up cold email based on the original email.\n\n"
            f"Context:\n"
            f"- Company: {cname_str}\n"
            f"- Original subject: {orig}\n"
            f"- Service/Offering: {off or 'driving more traffic'}\n"
            f"- Sender: {sn} from {sc}\n\n"
            f"Rules:\n"
            f"* Keep the email under 50 words total.\n"
            f"* Do NOT include any greeting, salutation, or opening line (no 'Hi', 'Hello', 'Dear', or recipient name).\n"
            f"* Start directly with: \"Just following up in case my last note got buried...\"\n"
            f"* Offer to share a couple of quick ideas for driving more traffic to {cname_str}.\n"
            f"* Make it clear that the ideas can be shared even if a call is not ideal right now.\n"
            f"* End with: \"Worth 10 minutes this week?\"\n"
            f"* Keep the tone friendly, natural, conversational, and non-pushy.\n"
            f"* Do not invent facts, statistics, results, or claims.\n"
            f"* Do not add a signature or sign-off block.\n"
            f"* Do NOT output code brackets or variable tokens like {{first_name}} or {{company_name}}.\n"
            f"* Return only valid JSON.\n\n"
            f"Respond ONLY with:\n"
            f"{{\n"
            f"  \"subject\": \"Re: {orig}\",\n"
            f"  \"body\": \"<follow-up email under 50 words>\"\n"
            f"}}"
        )
        raw, err = _smart_ai_call(key, prompt, _SYSTEM_PROMPT)
        if raw:
            sub, body = _parse_json_response(raw)
            if sub and body:
                sub = _clean_placeholders(sub, fn, cn)
                body = _clean_placeholders(body, fn, cn)
                return {"subject": sub, "body": body}

    idx      = min(follow_up_number - 1, len(_FOLLOWUP_TEMPLATES) - 1)
    st, bt   = _FOLLOWUP_TEMPLATES[idx]
    cname    = company_name if company_name else "your company"
    sub_text = st.format(original_subject=original_subject, company_name=cname)
    body_text = bt.format(company_name=cname, original_subject=original_subject)
    return {
        "subject": _clean_placeholders(sub_text, first_name, company_name),
        "body":    _clean_placeholders(body_text, first_name, company_name),
    }
