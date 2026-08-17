from __future__ import annotations

import secrets
from typing import List, Optional


def key_is_valid(provided: Optional[str], valid_keys: List[str]) -> bool:
    """Auth check, kept as a pure function so it's trivially testable offline.

    - No keys configured -> auth is DISABLED (returns True) — local dev / tests.
    - Keys configured -> the provided key must match one (constant-time compare).
    """
    if not valid_keys:
        return True
    if not provided:
        return False
    return any(secrets.compare_digest(provided, k) for k in valid_keys)
