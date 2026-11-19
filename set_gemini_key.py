"""Prompt for and store a Gemini API key in the operating system keychain."""

from getpass import getpass

import keyring
from keyring.errors import KeyringError

from gemini_credentials import KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE


def main() -> None:
    api_key = getpass("Gemini API key (input hidden): ").strip()
    if not api_key:
        raise SystemExit("No API key entered; nothing was saved.")

    try:
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, api_key)
    except KeyringError:
        raise SystemExit("Could not save the key to the system keychain.") from None

    print("Gemini API key saved to the system keychain.")


if __name__ == "__main__":
    main()