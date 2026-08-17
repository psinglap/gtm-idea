"""Encryption for stored credentials (Gmail refresh tokens, Apollo API keys).

Fernet (AES-128-CBC + HMAC-SHA256). Nothing reaches the database in clear text —
`Connection.secret` always holds ciphertext.

**You do not have to generate a key.** If `WG_SECRET_KEY` is unset, the server creates one on
first use and persists it (see `bootstrap`), so connecting Gmail is just "Sign in with Google"
with no setup step. Setting `WG_SECRET_KEY` in the environment is the stronger option and takes
precedence: it keeps the key somewhere the database does not, so a leaked database dump alone
cannot decrypt the tokens. The auto-generated key lives next to the data, which protects
against a stray backup or a curious read-replica but not against someone who has the database
itself. That is the honest trade, and it is the right default for a single-operator setup.

Key rotation: `WG_SECRET_KEY` may hold several comma-separated keys. The FIRST encrypts, ALL
decrypt. Rotate by prepending a new key, letting old rows decrypt, and re-saving them before
dropping the retired one.
"""
from __future__ import annotations

import os
from typing import Callable, List, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretKeyMissing(RuntimeError):
    """Raised only when no key is configured AND no auto-generated key can be reached."""


# Set by `bootstrap()` at startup: a callable returning the persisted key, creating it on first
# call. Kept as a hook so this module stays free of any storage dependency.
_key_provider: Optional[Callable[[], str]] = None


def bootstrap(provider: Callable[[], str]) -> None:
    """Install the fallback key source (normally backed by the store)."""
    global _key_provider
    _key_provider = provider


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _keys() -> List[str]:
    env = [k.strip() for k in os.getenv("WG_SECRET_KEY", "").split(",") if k.strip()]
    if env:
        return env
    if _key_provider is not None:
        auto = (_key_provider() or "").strip()
        if auto:
            return [auto]
    return []


def is_configured() -> bool:
    """True when secrets can be stored — which is now almost always, since a key is created on
    demand. The API still reports it so the UI can explain a genuinely broken setup."""
    return bool(_keys())


def _fernet() -> MultiFernet:
    keys = _keys()
    if not keys:
        raise SecretKeyMissing(
            "No encryption key available and none could be created. Set WG_SECRET_KEY, or check "
            "that the database is reachable so a key can be generated and stored."
        )
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError) as e:
        raise SecretKeyMissing(f"WG_SECRET_KEY is not a valid Fernet key: {e}") from e


def encrypt(plaintext: str) -> str:
    """Plaintext secret -> ciphertext for `Connection.secret`. Empty stays empty (session-based
    providers like Luma/LinkedIn hold no credential at all)."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Ciphertext -> plaintext. Returns "" for empty input.

    Raises InvalidToken when the value cannot be decrypted with ANY configured key — which in
    practice means the key was rotated away or the row was written under a different key. That
    surfaces as a reconnect prompt rather than a mystery 401 from the provider later.
    """
    if not ciphertext:
        return ""
    return _fernet().decrypt(ciphertext.encode()).decode()


def try_decrypt(ciphertext: str) -> str:
    """decrypt() that returns "" instead of raising — for read paths that should degrade to
    'disconnected' rather than blow up a whole cron run."""
    try:
        return decrypt(ciphertext)
    except (InvalidToken, SecretKeyMissing):
        return ""
