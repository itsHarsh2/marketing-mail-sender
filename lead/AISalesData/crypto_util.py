"""
crypto_util.py — encryption for stored SMTP credentials.

WHAT THIS PROTECTS AGAINST
    The sender password column in sales_campaign.db is encrypted, so the
    database file can be copied, backed up, emailed to support, or
    accidentally committed without handing over working mail credentials.

WHAT IT DOES *NOT* PROTECT AGAINST
    If the encryption key lives on the same machine as the database (the
    auto-generated keyfile mode below), anyone who can read both files can
    decrypt.  That is obfuscation-at-rest, not a secure vault.  To get real
    separation, put the key in .streamlit/secrets.toml or the
    SALES_SECRET_KEY environment variable and keep it out of your backups.

KEY RESOLUTION ORDER
    1. SALES_SECRET_KEY environment variable
    2. st.secrets["sender_encryption_key"]      (.streamlit/secrets.toml)
    3. auto-generated keyfile, chmod 0600, next to the database

Generate a key for options 1 or 2 with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import os
import stat

from cryptography.fernet import Fernet, InvalidToken

from utils import log_info, log_warning, log_error

# Where the auto-generated fallback key is written.
_KEYFILE = os.environ.get(
    "SALES_SECRET_KEY_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sender_key"),
)

_SECRET_NAME = "sender_encryption_key"

# Cached so we don't re-read the keyfile on every Streamlit rerun.
_fernet: Fernet | None = None
_key_source: str = "none"


def _read_streamlit_secret() -> str | None:
    """
    Read the key from .streamlit/secrets.toml, if a secrets file exists.

    st.secrets raises StreamlitSecretNotFoundError when no secrets file is
    present anywhere — including from .get() — so this must be guarded
    rather than relying on a default value.
    """
    try:
        import streamlit as st
        value = st.secrets[_SECRET_NAME]
        return str(value).strip() or None
    except Exception:
        return None


def _load_or_create_keyfile() -> bytes:
    """Return the fallback key, creating it with 0600 perms on first use."""
    if os.path.exists(_KEYFILE):
        with open(_KEYFILE, "rb") as fh:
            return fh.read().strip()

    key = Fernet.generate_key()
    with open(_KEYFILE, "wb") as fh:
        fh.write(key)
    try:
        os.chmod(_KEYFILE, stat.S_IRUSR | stat.S_IWUSR)   # 0600, owner only
    except OSError:
        log_warning(f"Could not restrict permissions on {_KEYFILE}.")
    log_info(f"Generated a new credential encryption key at {_KEYFILE}")
    return key


def _get_fernet() -> Fernet:
    """Resolve the encryption key once and cache the cipher."""
    global _fernet, _key_source
    if _fernet is not None:
        return _fernet

    key = (os.environ.get("SALES_SECRET_KEY") or "").strip()
    source = "environment variable SALES_SECRET_KEY"

    if not key:
        key = _read_streamlit_secret()
        source = "st.secrets (.streamlit/secrets.toml)"

    if not key:
        key = _load_or_create_keyfile()
        source = f"local keyfile ({_KEYFILE})"

    if isinstance(key, str):
        key = key.encode()

    try:
        _fernet = Fernet(key)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"The credential encryption key from {source} is not a valid "
            "Fernet key. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        ) from exc

    _key_source = source
    return _fernet


def key_source() -> str:
    """Human-readable description of where the active key came from."""
    _get_fernet()
    return _key_source


def uses_local_keyfile() -> bool:
    """True when falling back to the co-located keyfile (weaker protection)."""
    _get_fernet()
    return _key_source.startswith("local keyfile")


def encrypt(plaintext: str) -> bytes:
    """Encrypt a secret for storage. Empty input stays empty."""
    if not plaintext:
        return b""
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes | str | None) -> str:
    """
    Decrypt a stored secret.

    Returns "" if the value is empty or cannot be decrypted — a rotated or
    lost key must not crash the app, it should just present the account as
    needing its password re-entered.
    """
    if not token:
        return ""
    if isinstance(token, str):
        token = token.encode()
    try:
        return _get_fernet().decrypt(token).decode("utf-8")
    except InvalidToken:
        log_warning(
            "A stored credential could not be decrypted — the encryption key "
            "has changed or the record was written with a different key."
        )
        return ""
    except Exception as exc:
        log_error(f"Unexpected error decrypting a stored credential: {exc}")
        return ""
