# -*- coding: utf-8 -*-
"""Encrypts request text before it is written to the database.

Prompts hold source code and conversation content, so they are stored
encrypted. The key lives in the .env file, not in the database, so a copy of
dashboard.sqlite3 on its own reveals nothing.

The key is generated on first run and appended to .env. If the key is missing or
wrong, stored text is unreadable and the dashboard shows the request without it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

ENV_VAR = "LOG_ENCRYPTION_KEY"
ENV_FILE = Path(".env")

_cipher: Optional[Fernet] = None
_checked = False


def _read_key() -> Optional[str]:
    key = (os.getenv(ENV_VAR) or "").strip()
    return key or None


def _append_to_env(key: str) -> bool:
    """Append the generated key to .env. Returns False if it could not be written."""
    try:
        existing = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
        if re.search(rf"(?m)^{ENV_VAR}=", existing):
            return True
        separator = "" if existing.endswith("\n") or not existing else "\n"
        with ENV_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{separator}\n# Generated on first run. Losing it makes stored prompts unreadable.\n")
            handle.write(f"{ENV_VAR}={key}\n")
        return True
    except OSError as exc:
        logger.warning(f"[Logs] Could not write {ENV_VAR} to .env: {exc}")
        return False


def ensure_key() -> Optional[str]:
    """Return the key, generating and persisting one on first run."""
    key = _read_key()
    if key:
        return key

    key = Fernet.generate_key().decode()
    os.environ[ENV_VAR] = key
    if _append_to_env(key):
        logger.info(f"[Logs] Generated {ENV_VAR} and saved it to .env")
    else:
        logger.warning(
            f"[Logs] Generated {ENV_VAR} but could not save it. "
            "Set it manually or stored prompts will be unreadable after a restart."
        )
    return key


def _get_cipher() -> Optional[Fernet]:
    global _cipher, _checked
    if _cipher is not None:
        return _cipher
    if _checked:
        return None
    _checked = True
    key = _read_key()
    if not key:
        return None
    try:
        _cipher = Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        logger.warning(f"[Logs] {ENV_VAR} is not a valid key, text will not be stored: {exc}")
        return None
    return _cipher


def reset_cache() -> None:
    """Forget the loaded key. Used by tests and after the key changes."""
    global _cipher, _checked
    _cipher = None
    _checked = False


def available() -> bool:
    return _get_cipher() is not None


def encrypt(text: Optional[str]) -> Optional[bytes]:
    """Encrypt text, or return None when there is nothing to store."""
    if not text:
        return None
    cipher = _get_cipher()
    if cipher is None:
        return None
    return cipher.encrypt(text.encode("utf-8"))


def decrypt(blob: Optional[bytes]) -> Optional[str]:
    """Decrypt stored text. Returns None when the key is missing or wrong."""
    if not blob:
        return None
    cipher = _get_cipher()
    if cipher is None:
        return None
    try:
        return cipher.decrypt(bytes(blob)).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
