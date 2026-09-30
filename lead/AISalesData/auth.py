"""
auth.py — password gate and persistent session manager for the app.

Features:
- Single shared password verified via PBKDF2
- 24-hour persistent SQLite session tokens surviving browser reloads / refreshes
- Automatic cleanup on sign out or expiry
"""

import hashlib
import hmac
import os
import secrets as _secrets
import time

import streamlit as st
from database import (
    create_user_session,
    validate_user_session,
    delete_user_session,
)

_ALGO        = "pbkdf2_sha256"
_ITERATIONS  = 200_000
_SALT_BYTES  = 16
_SECRET_NAME = "app_password_hash"

# Rate-limiting / lockout settings
_MAX_ATTEMPTS = 5
_LOCKOUT_SECS = 60
_SESSION_HOURS = 24


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    """Return a self-describing hash string safe to store in a config file."""
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash."""
    try:
        algo, iters, salt_hex, hash_hex = stored.strip().split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(iters),
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _stored_hash() -> str:
    """Read the hash from secrets.toml or the environment."""
    env = (os.environ.get("APP_PASSWORD_HASH") or "").strip()
    if env:
        return env
    try:
        return str(st.secrets[_SECRET_NAME]).strip()
    except Exception:
        return ""


def _clear_auth_state() -> None:
    """Clear session token from database, query parameters, and session_state."""
    token = st.session_state.get("_auth_token") or st.query_params.get("session")
    if token:
        delete_user_session(token)
    try:
        st.query_params.pop("session", None)
    except Exception:
        pass
    for k in ("_authed", "_auth_token", "_auth_attempts", "_auth_last_activity"):
        st.session_state.pop(k, None)


def _render_login(stored: str) -> None:
    """Draw the login form. Never returns normally — always stops the script."""
    _, col, _ = st.columns([1, 1.2, 1])
    with col:
        st.markdown("# 🔒 AI Sales Machine")
        st.caption("This tool can send email from your accounts. Sign in to continue.")

        now = time.time()
        locked_until = st.session_state.get("_auth_locked_until", 0)
        if now < locked_until:
            st.error(
                f"Too many failed attempts. Try again in "
                f"{int(locked_until - now)} seconds."
            )
            st.stop()

        with st.form("login_form"):
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

        if submitted:
            if verify_password(password, stored):
                token = _secrets.token_hex(32)
                create_user_session(token, hours=_SESSION_HOURS)
                st.query_params["session"] = token
                st.session_state["_authed"] = True
                st.session_state["_auth_token"] = token
                st.session_state["_auth_attempts"] = 0
                st.session_state["_auth_last_activity"] = now
                st.rerun()
            else:
                attempts = st.session_state.get("_auth_attempts", 0) + 1
                st.session_state["_auth_attempts"] = attempts
                if attempts >= _MAX_ATTEMPTS:
                    st.session_state["_auth_locked_until"] = now + _LOCKOUT_SECS
                    st.error("Too many failed attempts. Locked for a minute.")
                else:
                    st.error(
                        f"Incorrect password. "
                        f"{_MAX_ATTEMPTS - attempts} attempt(s) left."
                    )

        st.stop()


def _secrets_path() -> str:
    """Where secrets.toml lives, next to the app."""
    base = os.environ.get("AISALES_HOME") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ".streamlit", "secrets.toml")


def _write_secret_line(key: str, value: str) -> None:
    """Append or replace one key in secrets.toml, creating the file if needed."""
    path = _secrets_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines()
                     if not l.strip().startswith(f"{key}")]
    lines.append(f'{key} = "{value}"')
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _render_first_run_setup() -> None:
    """First-time password setup for local desktop installations."""
    _, col, _ = st.columns([1, 1.2, 1])
    with col:
        st.markdown("# 👋 Welcome to AI Sales Machine")
        st.markdown(
            "First run on this computer. Choose a password — it protects an app "
            "that can send email from your accounts and holds your contact data."
        )

        with st.form("first_run"):
            p1 = st.text_input("Create a password", type="password")
            p2 = st.text_input("Confirm password", type="password")
            go = st.form_submit_button("Save and continue", type="primary", use_container_width=True)

        if go:
            if len(p1) < 8:
                st.error("Use at least 8 characters.")
            elif p1 != p2:
                st.error("The two passwords do not match.")
            else:
                try:
                    _write_secret_line("app_password_hash", hash_password(p1))
                    try:
                        st.secrets["sender_encryption_key"]
                    except Exception:
                        from cryptography.fernet import Fernet
                        _write_secret_line("sender_encryption_key",
                                           Fernet.generate_key().decode())
                    token = _secrets.token_hex(32)
                    create_user_session(token, hours=_SESSION_HOURS)
                    st.query_params["session"] = token
                    st.session_state["_authed"] = True
                    st.session_state["_auth_token"] = token
                    st.session_state["_auth_last_activity"] = time.time()
                    st.success("Password saved. Loading…")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not save the password: {exc}")
                    st.caption(f"Tried to write {_secrets_path()}")

        st.stop()


def require_login() -> None:
    """
    Gate every page. Call once, immediately after st.set_page_config().
    
    Checks in-memory active session or persistent SQLite session token from URL.
    """
    now = time.time()

    # 1. In-memory active tab session check
    if st.session_state.get("_authed"):
        token = st.session_state.get("_auth_token") or st.query_params.get("session")
        if token and validate_user_session(token):
            st.session_state["_auth_last_activity"] = now
            return
        # If token was deleted or expired
        _clear_auth_state()
        st.warning("⏱️ Session timed out or expired. Please sign in again.")
        _render_login(_stored_hash())
        return

    # 2. Browser refresh / re-open check via query parameter token
    token = st.query_params.get("session")
    if token and validate_user_session(token):
        st.session_state["_authed"] = True
        st.session_state["_auth_token"] = token
        st.session_state["_auth_last_activity"] = now
        return

    # 3. Prompt for login
    stored = _stored_hash()
    if not stored:
        if os.environ.get("AISALES_ALLOW_SETUP") == "1":
            _render_first_run_setup()
        st.error("🔒 No application password is configured.")
        st.markdown(
            "This app can send email from your accounts, so it will not run "
            "without one.\n\n"
            "**Set one:**\n"
            "```bash\n"
            "python set_password.py\n"
            "```\n"
            "then paste the printed line into `.streamlit/secrets.toml` "
            "(or set the `APP_PASSWORD_HASH` environment variable) and "
            "restart."
        )
        st.stop()

    _render_login(stored)


def render_logout(container=None) -> None:
    """Sign-out control placed in the sidebar."""
    target = container or st
    if target.button("🔓 Sign out", use_container_width=True, key="_logout_btn"):
        _clear_auth_state()
        st.rerun()
