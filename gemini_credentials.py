"""Retrieve Gemini credentials from macOS Keychain or an explicit environment variable."""

import os

import keyring
from keyring.errors import KeyringError

KEYCHAIN_SERVICE = "ai-resume-to-jd-screener"
KEYCHAIN_ACCOUNT = "GEMINI_API_KEY"


def get_gemini_api_key() -> str | None:
    """Prefer an explicitly supplied environment variable, then the OS keychain."""
    environment_key = os.getenv("GEMINI_API_KEY", "").strip()
    if environment_key:
        return environment_key

    try:
        return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    except KeyringError:
        return None