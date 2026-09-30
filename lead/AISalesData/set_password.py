"""
set_password.py — generate the app login password hash.

Usage:
    python set_password.py

Prompts for a password (hidden), prints the line to paste into
.streamlit/secrets.toml. The password itself is never stored anywhere;
only the PBKDF2 hash is.
"""

import getpass
import sys

from auth import hash_password

MIN_LENGTH = 10


def main() -> int:
    print("Set the login password for AI Sales Machine.\n")
    pw1 = getpass.getpass("New password: ")
    if len(pw1) < MIN_LENGTH:
        print(f"\n✗ Too short — use at least {MIN_LENGTH} characters.")
        print("  This guards live sending accounts on a public IP.")
        return 1

    pw2 = getpass.getpass("Confirm password: ")
    if pw1 != pw2:
        print("\n✗ Passwords do not match.")
        return 1

    print("\n✓ Add this line to .streamlit/secrets.toml:\n")
    print(f'app_password_hash = "{hash_password(pw1)}"')
    print("\nThen restart the app. The password itself is not stored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
