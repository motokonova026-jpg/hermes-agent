"""Story-alias validation for the story_doc plugin.

A ``story_key`` is a user-chosen short identifier that names one Google Doc.
Discord users type the alias on every command (``!story start cyberpunk-noir
...``) so the model never has to invent or remember Discord channel/thread
ids -- which is necessary because Hermes' tool dispatch path (model_tools.py)
does not currently surface chat context to tool handlers.

Rules:

* lowercase a-z, 0-9, ``-``, ``_``
* must start with a letter or digit
* 1 to 64 characters
* must not begin with a recognised platform-id prefix (``discord:``,
  ``telegram:``, ``slack:``, ``matrix:``) -- defence-in-depth in case the
  model tries to pass a raw chat identifier through

This module is pure: no I/O, no plugin imports. Easy to unit-test and easy
to reuse from later PRs without dragging the SQLite store along.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# Pattern matches the full normalised key. Compiled once at import time.
_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_LEN = 64

# Lowercased prefixes that look like platform-scoped ids. Defensive only --
# the model should never pass these as story_keys, but if it does we want a
# clear, actionable error rather than silently writing a nonsense alias.
_RESERVED_PREFIXES: Tuple[str, ...] = (
    "discord:",
    "telegram:",
    "slack:",
    "matrix:",
    "signal:",
    "imessage:",
    "whatsapp:",
)


def validate_story_key(value: object) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(story_key, None)`` on success, ``(None, error)`` on failure.

    The error message is user-actionable -- it tells the caller what to do,
    not just that something was wrong. Callers can surface the message
    verbatim to the user (CLI, tool_error JSON, etc.).

    Surrounding whitespace is stripped before validation. The original
    casing is checked exactly: uppercase input is rejected with a hint
    suggesting the lowercased form.
    """
    if value is None:
        return None, "story_key is required (e.g. 'cyberpunk-noir')"
    if not isinstance(value, str):
        return None, (
            f"story_key must be a string, got {type(value).__name__}"
        )

    s = value.strip()
    if not s:
        return None, "story_key is required (e.g. 'cyberpunk-noir')"

    # Reject platform-id prefixes (case-insensitive on the prefix itself).
    lowered = s.lower()
    for prefix in _RESERVED_PREFIXES:
        if lowered.startswith(prefix):
            return None, (
                f"story_key looks like a platform id ('{prefix}...'); "
                "pick a human-readable alias instead, e.g. 'cyberpunk-noir' "
                "or 'thursday-bedtime-story'"
            )

    if len(s) > _MAX_LEN:
        return None, (
            f"story_key is too long ({len(s)} chars); max is {_MAX_LEN}"
        )

    if _PATTERN.match(s):
        return s, None

    # Failed the pattern -- try to give a specific, helpful error.
    if s != s.lower():
        return None, (
            "story_key must be lowercase; "
            f"try '{s.lower()}' instead"
        )
    if s[0] in "-_":
        return None, (
            "story_key must start with a letter or digit, not '-' or '_'"
        )

    # Find the first character outside the allowed set so we can name it.
    allowed = re.compile(r"[a-z0-9_-]")
    for ch in s:
        if not allowed.match(ch):
            return None, (
                f"story_key contains invalid character {ch!r}; only "
                "lowercase letters, digits, '-' and '_' are allowed"
            )

    # Fallback (shouldn't normally reach here, but keep the contract honest).
    return None, (
        "story_key must match [a-z0-9][a-z0-9_-]{0,63} "
        f"(got {s!r})"
    )
