"""Agent-facing tools for the story_doc plugin.

PR #4 surface (this release):

  * ``story_doc_create`` (PR #2)
  * ``story_doc_read``    -- mode "full" or "tail" (PR #3)
  * ``story_doc_append``  -- append content to an existing alias's doc (PR #3)
  * ``story_doc_status``  -- alias metadata + fresh revision id (PR #3)
  * ``story_doc_revise``  -- mode "replace_body" or "replace_match" (NEW)

Out of scope (deferred):

  * ``story_doc_outline`` -- long-story navigation. Will land when Momo
    has stories long enough to need section-level navigation.
  * ``replace_range`` mode for revise -- agent-supplied character
    indices are too easy to get wrong. ``replace_match`` with the
    exact-once guard covers the surgical-edit use case more safely.

Important contract decisions baked in:

* **No ``check_fn``.** The tool is registered unconditionally so the model
  always sees its schema. Auth checks happen inside the handler and
  return a structured ``tool_error`` JSON the model can read aloud
  (verified in v2 plan section 1: ``get_definitions()`` filters
  False-checking tools out of the model's schema entirely, which would
  prevent Momo from even *responding* to ``!story start ...``).

* **Schema description carries the dispatch hint.** Plugin SKILL.md is
  not auto-loaded, so the only way to teach the model "when the user
  types ``!story start <alias> <prompt>``, call this" is to put the
  trigger directly in the schema description (which the model sees
  every turn). Verified in v2 plan section 1.

* **Explicit ``story_key`` argument.** Rob's PR #2 guidance: every
  command except ``!story list`` requires an alias. We don't try to
  infer one from Discord context (``handle_function_call`` doesn't
  forward chat IDs to tool handlers).

Auth-error contract (matches v2 plan section 5):

  Returned JSON shape on auth failure::

    {
      "error": "story_doc_<state>",
      "setup_step": "client_secret" | "auth" | "scope" | "refresh",
      "hint": "Run: hermes -p <profile> story setup --..."
    }

  The model parses the structured error and tells the Discord user the
  exact command to run.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from plugins.story_doc.aliases import validate_story_key
from plugins.story_doc.client import GoogleDocsClient, GoogleDocsError
from plugins.story_doc import oauth as _oauth
from plugins.story_doc.store import StoryDocError, StoryDocStore
from tools.registry import tool_result

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured-error helper
# ---------------------------------------------------------------------------
#
# Our auth-error contract (v2 plan section 5) puts a machine-readable
# error_key in "error" and the human-readable hint in "hint". We can't
# reuse ``tools.registry.tool_error`` directly for that shape because
# it puts the human message in "error" -- passing ``error=key`` would
# overwrite the message and we'd lose it. _struct_error keeps the
# convention consistent across all story_doc handlers.

def _struct_error(error_key: str, hint: str, **extra: Any) -> str:
    """Return ``{"error": <machine_key>, "hint": <human_msg>, **extra}`` JSON."""
    payload: Dict[str, Any] = {"error": error_key, "hint": hint}
    for k, v in extra.items():
        if v is not None:
            payload[k] = v
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

STORY_DOC_CREATE_SCHEMA: Dict[str, Any] = {
    "name": "story_doc_create",
    "description": (
        "Create a new Google Doc seeded with the supplied content, and "
        "register the alias -> document_id mapping in the local store. "
        "Returns {doc_id, url, word_count, story_key} on success.\n\n"
        "Trigger: when the Discord user message starts with "
        "`!story start <alias> <prompt>`, call this tool with "
        "story_key=<alias>, title=<a short title you derive from the "
        "prompt or the first sentence>, and content=<the opening draft "
        "you generate from the prompt>. The alias is mandatory and must "
        "match [a-z0-9][a-z0-9_-]{0,63}.\n\n"
        "If the alias already exists, this tool errors -- use "
        "`story_doc_append` (PR #3) or `story_doc_revise` (PR #3) to "
        "modify an existing story instead.\n\n"
        "If Google auth isn't configured, this tool returns a structured "
        "tool_error explaining what `hermes story setup ...` command to "
        "run; surface that hint to the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "story_key": {
                "type": "string",
                "description": (
                    "Short user-chosen alias for this story. Lowercase, "
                    "alphanumeric + '-' / '_', 1-64 chars, must start "
                    "with a letter or digit. Examples: 'cyberpunk-noir', "
                    "'thursday-bedtime-story', 'ch1-draft'."
                ),
            },
            "title": {
                "type": "string",
                "description": (
                    "Title for the Google Doc. Visible in Drive and at "
                    "the top of the doc. Keep it short -- the alias is "
                    "the canonical identifier; the title is for humans."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Initial body text for the doc. This is the first "
                    "draft you generated from the user's prompt. Plain "
                    "text only at v1 (Markdown is NOT rendered)."
                ),
            },
        },
        "required": ["story_key", "title", "content"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Auth-error mapper (single source of truth for tool_error shapes)
# ---------------------------------------------------------------------------

def _auth_check() -> Optional[str]:
    """Return a structured-error JSON when auth isn't ready; else None.

    Single source of truth for the auth-error JSON shape so handlers
    don't drift. The ``error`` field is a machine-readable key
    (e.g. ``story_doc_not_authenticated``); the ``hint`` field carries
    the actionable instructions surfaced by ``oauth.check_status``.
    """
    status = _oauth.check_status()
    if status["ok"]:
        return None

    state = status.get("state", "unknown")
    error_key = {
        "missing_client_secret": "story_doc_not_configured",
        "missing_token": "story_doc_not_authenticated",
        "missing_scopes": "story_doc_insufficient_scope",
        "refresh_failed": "story_doc_refresh_failed",
        "token_invalid": "story_doc_token_invalid",
    }.get(state, "story_doc_not_configured")
    setup_step = {
        "missing_client_secret": "client_secret",
        "missing_token": "auth",
        "missing_scopes": "scope",
        "refresh_failed": "refresh",
        "token_invalid": "auth",
    }.get(state, "client_secret")

    extra: Dict[str, Any] = {"setup_step": setup_step}
    if state == "missing_scopes":
        extra["missing"] = status.get("missing_scopes", [])

    return _struct_error(error_key, status.get("message", ""), **extra)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_story_doc_create(args: dict, **_kwargs) -> str:
    """Create a Google Doc, insert content, persist alias mapping.

    See ``STORY_DOC_CREATE_SCHEMA`` for argument contract.
    """
    # 1. Validate alias (cheap, deterministic, runs before auth so the
    #    user gets feedback on a bad alias even when not configured).
    raw_key = args.get("story_key")
    story_key, alias_err = validate_story_key(raw_key)
    if alias_err:
        return _struct_error("invalid_story_key", alias_err)

    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        return _struct_error(
            "invalid_title",
            "title is required and must be a non-empty string",
        )

    content = args.get("content")
    if not isinstance(content, str):
        return _struct_error(
            "invalid_content",
            f"content must be a string, got {type(content).__name__}",
        )

    # 2. Auth check (returns structured-error JSON when not ready).
    if (auth_err := _auth_check()) is not None:
        return auth_err

    # 3. Pre-check the alias isn't already taken so we don't create a
    #    Drive doc and orphan it.
    store = StoryDocStore()
    try:
        existing = store.get_story(story_key)
        if existing is not None:
            return _struct_error(
                "story_key_exists",
                f"story_key '{story_key}' already exists; pick a different "
                "alias, or use story_doc_append / story_doc_revise (PR #3) "
                "to modify the existing story",
                story_key=story_key,
                doc_id=existing.get("doc_id"),
            )

        # 4. Create the doc + insert content via the Docs API.
        client = GoogleDocsClient()
        try:
            doc_id = client.create_doc(title.strip())
            inserted = client.insert_text_at_end(doc_id, content)
        except GoogleDocsError as exc:
            return _struct_error(
                "google_docs_error",
                str(exc),
                status_code=exc.status_code,
            )

        # 5. Best-effort revision id (informational; failure is non-fatal
        #    inside the client).
        revision_id = client.get_latest_revision_id(doc_id)

        # 6. Persist mapping.
        try:
            store.upsert_story(
                story_key=story_key,
                doc_id=doc_id,
                title=title.strip(),
                word_count=_word_count(content),
                last_revision_id=revision_id,
                mode="create",
            )
        except StoryDocError as exc:
            # Race: another caller created the same alias between our
            # pre-check and the insert. The Doc exists in Drive but isn't
            # mapped -- surface a partial-success error so the user can
            # recover (probably by deleting the orphan from Drive).
            return _struct_error(
                "store_persist_failed",
                f"created Google Doc {doc_id} but failed to register "
                f"alias '{story_key}': {exc}. The doc URL is "
                f"{client.web_url(doc_id)} -- you may want to delete it "
                "from Drive and try again with a different alias.",
                doc_id=doc_id,
                url=client.web_url(doc_id),
            )
    finally:
        store.close()

    return tool_result({
        "story_key": story_key,
        "doc_id": doc_id,
        "url": client.web_url(doc_id),
        "title": title.strip(),
        "word_count": _word_count(content),
        "characters_inserted": inserted,
        "revision_id": revision_id,
    })


# ===========================================================================
# story_doc_read
# ===========================================================================

STORY_DOC_READ_SCHEMA: Dict[str, Any] = {
    "name": "story_doc_read",
    "description": (
        "Fetch the current text of an existing story by alias. "
        "Use mode='full' (default) to return the entire document, "
        "or mode='tail' with tail_chars=N to return only the last N "
        "characters (useful when the doc is long and you only need the "
        "recent context to extend it).\n\n"
        "Trigger: this is the read step before responding to "
        "`!story continue <alias> <instruction>`. Call story_doc_read "
        "first with the alias from the user's message, then generate the "
        "next chunk with the returned text in mind, then call "
        "story_doc_append to write it back.\n\n"
        "Errors include `story_key_not_found` (alias doesn't exist; tell "
        "the user to run `!story list` or `!story start`), plus the "
        "shared auth-error contract."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "story_key": {
                "type": "string",
                "description": "Existing story alias.",
            },
            "mode": {
                "type": "string",
                "enum": ["full", "tail"],
                "description": (
                    "full (default): return the entire document body. "
                    "tail: return only the last `tail_chars` characters."
                ),
            },
            "tail_chars": {
                "type": "integer",
                "description": (
                    "For mode='tail': number of trailing characters to "
                    "return. Default 4000. Ignored for mode='full'."
                ),
            },
        },
        "required": ["story_key"],
        "additionalProperties": False,
    },
}


def handle_story_doc_read(args: dict, **_kwargs) -> str:
    raw_key = args.get("story_key")
    story_key, alias_err = validate_story_key(raw_key)
    if alias_err:
        return _struct_error("invalid_story_key", alias_err)

    mode = (args.get("mode") or "full").strip().lower()
    if mode not in ("full", "tail"):
        return _struct_error(
            "invalid_mode",
            f"mode must be 'full' or 'tail', got {mode!r}",
        )

    tail_chars_raw = args.get("tail_chars", 4000)
    try:
        tail_chars = int(tail_chars_raw)
    except (TypeError, ValueError):
        return _struct_error(
            "invalid_tail_chars",
            f"tail_chars must be an integer, got {tail_chars_raw!r}",
        )
    if tail_chars < 1:
        return _struct_error(
            "invalid_tail_chars",
            f"tail_chars must be a positive integer, got {tail_chars}",
        )

    if (auth_err := _auth_check()) is not None:
        return auth_err

    row, lookup_err = _resolve_story_or_error(story_key)
    if lookup_err:
        return lookup_err

    doc_id = row["doc_id"]
    client = GoogleDocsClient()
    try:
        text = client.read_doc_text(doc_id)
    except GoogleDocsError as exc:
        return _struct_error(
            "google_docs_error",
            str(exc),
            status_code=exc.status_code,
        )

    body: Dict[str, Any] = {
        "story_key": story_key,
        "doc_id": doc_id,
        "url": client.web_url(doc_id),
        "title": row.get("title"),
        "mode": mode,
        "char_count": len(text),
        "word_count": _word_count(text),
    }
    if mode == "tail":
        body["text"] = text[-tail_chars:]
        body["truncated"] = len(text) > tail_chars
        body["tail_chars"] = tail_chars
    else:
        body["text"] = text
        body["truncated"] = False

    return tool_result(body)


# ===========================================================================
# story_doc_append
# ===========================================================================

STORY_DOC_APPEND_SCHEMA: Dict[str, Any] = {
    "name": "story_doc_append",
    "description": (
        "Append `content` to the end of an existing story's Google Doc. "
        "By default a paragraph break (`\\n\\n`) is inserted before the "
        "new content so passages don't run together; pass separator='' "
        "to suppress it.\n\n"
        "Trigger: when the user types `!story continue <alias> "
        "<instruction>`, call story_doc_read first to load the current "
        "text, generate the next chunk in your reply that respects the "
        "instruction, then call story_doc_append with story_key=<alias> "
        "and content=<your generated chunk>.\n\n"
        "Updates the local word_count and last_revision_id in the alias "
        "store. Errors include `story_key_not_found` plus the shared "
        "auth-error contract."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "story_key": {
                "type": "string",
                "description": "Existing story alias.",
            },
            "content": {
                "type": "string",
                "description": (
                    "Text to append. Plain text only at v1 (Markdown is "
                    "NOT rendered)."
                ),
            },
            "separator": {
                "type": "string",
                "description": (
                    "Inserted between the existing doc end and the new "
                    "content. Default '\\n\\n' (paragraph break). Pass "
                    "an empty string '' to suppress."
                ),
            },
        },
        "required": ["story_key", "content"],
        "additionalProperties": False,
    },
}

# Default separator. Centralised so the schema description and the
# handler don't drift.
_DEFAULT_APPEND_SEPARATOR = "\n\n"


def handle_story_doc_append(args: dict, **_kwargs) -> str:
    raw_key = args.get("story_key")
    story_key, alias_err = validate_story_key(raw_key)
    if alias_err:
        return _struct_error("invalid_story_key", alias_err)

    content = args.get("content")
    if not isinstance(content, str):
        return _struct_error(
            "invalid_content",
            f"content must be a string, got {type(content).__name__}",
        )
    if content == "":
        return _struct_error(
            "invalid_content",
            "content must not be empty -- there is nothing to append",
        )

    raw_separator = args.get("separator")
    if raw_separator is None:
        separator = _DEFAULT_APPEND_SEPARATOR
    elif isinstance(raw_separator, str):
        separator = raw_separator
    else:
        return _struct_error(
            "invalid_separator",
            f"separator must be a string, got {type(raw_separator).__name__}",
        )

    if (auth_err := _auth_check()) is not None:
        return auth_err

    row, lookup_err = _resolve_story_or_error(story_key)
    if lookup_err:
        return lookup_err

    doc_id = row["doc_id"]
    payload = (separator + content) if separator else content

    client = GoogleDocsClient()
    try:
        chars_inserted = client.insert_text_at_end(doc_id, payload)
    except GoogleDocsError as exc:
        return _struct_error(
            "google_docs_error",
            str(exc),
            status_code=exc.status_code,
        )
    revision_id = client.get_latest_revision_id(doc_id)

    # Persist the post-append metadata: bump word count by the *content*
    # word count (separators aren't words), refresh revision_id, stamp
    # updated_at via upsert.
    appended_word_count = _word_count(content)
    store = StoryDocStore()
    try:
        new_total = row.get("word_count", 0)
        try:
            new_total = store.bump_word_count(story_key, appended_word_count)
        except StoryDocError as exc:
            # Word count bump failure isn't fatal for the user -- the
            # text is already in Drive. Surface a soft warning so the
            # model can mention it.
            logger.warning(
                "bump_word_count failed for %s: %s", story_key, exc
            )
        try:
            store.upsert_story(
                story_key=story_key,
                doc_id=doc_id,
                last_revision_id=revision_id,
                mode="update",
            )
        except StoryDocError as exc:
            logger.warning(
                "post-append store update failed for %s: %s",
                story_key, exc,
            )
    finally:
        store.close()

    return tool_result({
        "story_key": story_key,
        "doc_id": doc_id,
        "url": client.web_url(doc_id),
        "title": row.get("title"),
        "characters_inserted": chars_inserted,
        "appended_word_count": appended_word_count,
        "total_word_count": new_total,
        "revision_id": revision_id,
        "separator_used": separator,
    })


# ===========================================================================
# story_doc_status
# ===========================================================================

STORY_DOC_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "story_doc_status",
    "description": (
        "Return metadata about an existing story by alias: doc_id, URL, "
        "title, word_count, created_at, updated_at, last_revision_id, "
        "and (if Drive is reachable) the freshly-fetched revision_id "
        "from the API.\n\n"
        "Useful when the user asks 'how long is my story?' or 'where is "
        "my doc?'. Doesn't fetch the body text -- use story_doc_read for "
        "that.\n\n"
        "Errors: `story_key_not_found` plus the shared auth-error "
        "contract (auth is required because we hit Drive for the "
        "current revision id; if you only need the cached fields and "
        "auth is broken, the local row is included in the error payload "
        "as `cached_row`)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "story_key": {
                "type": "string",
                "description": "Existing story alias.",
            },
        },
        "required": ["story_key"],
        "additionalProperties": False,
    },
}


def handle_story_doc_status(args: dict, **_kwargs) -> str:
    raw_key = args.get("story_key")
    story_key, alias_err = validate_story_key(raw_key)
    if alias_err:
        return _struct_error("invalid_story_key", alias_err)

    # Resolve the alias FIRST so a missing alias returns a clear error
    # without bothering the user to set up auth.
    row, lookup_err = _resolve_story_or_error(story_key)
    if lookup_err:
        return lookup_err

    cached_row = _row_to_status(story_key, row)

    # Now check auth. If not ready, return an auth error but include the
    # cached metadata so the caller still gets something useful.
    if (auth_err := _auth_check()) is not None:
        # _auth_check returns a JSON string; parse it, add cached_row,
        # re-emit. Keeps the contract consistent.
        try:
            payload = json.loads(auth_err)
        except json.JSONDecodeError:
            return auth_err
        payload["cached_row"] = cached_row
        return json.dumps(payload, ensure_ascii=False)

    doc_id = row["doc_id"]
    client = GoogleDocsClient()
    fresh_revision = client.get_latest_revision_id(doc_id)

    body = dict(cached_row)
    body["url"] = client.web_url(doc_id)
    if fresh_revision is not None:
        body["fresh_revision_id"] = fresh_revision
        # If Drive's latest differs from what we cached, surface that --
        # someone (or the API) edited the doc out of band.
        body["revision_drift"] = (
            cached_row.get("last_revision_id") != fresh_revision
        )
    else:
        body["fresh_revision_id"] = None
        body["revision_drift"] = None  # unknown
    return tool_result(body)


# ===========================================================================
# story_doc_revise
# ===========================================================================

STORY_DOC_REVISE_SCHEMA: Dict[str, Any] = {
    "name": "story_doc_revise",
    "description": (
        "Edit an existing story's Google Doc. The DISCORD TRIGGER "
        "determines which combination of (operation_intent, mode) "
        "you must use -- DO NOT pick freely:\n\n"
        "  `!story revise <alias> <instruction>` ->\n"
        "      operation_intent='targeted_revise', mode='replace_match'\n"
        "      (anchor-based surgical edit; preserves the rest of the doc)\n\n"
        "  `!story update <alias> <instruction>` ->\n"
        "      operation_intent='broad_update',  mode='replace_body'\n"
        "      (full rewrite; replaces the entire body)\n\n"
        "The handler enforces (operation_intent, mode) coupling and "
        "rejects mismatches with `intent_mode_mismatch`. The two "
        "combinations above are the ONLY legal pairings.\n\n"
        "**SAFETY: do NOT route around `revise_match_ambiguous`.** "
        "If the user typed `!story revise` and "
        "`mode=replace_match` returns `revise_match_ambiguous`, you "
        "must surface that error verbatim to the user (so they can "
        "pick a more specific snippet). You may NOT switch to "
        "`operation_intent=broad_update` + `mode=replace_body` to "
        "force a global substitution -- that's a deliberate bypass "
        "of the unique-match guard and will produce a confusing "
        "full-rewrite when the user asked for a targeted edit. The "
        "only legitimate `broad_update` is when the user explicitly "
        "typed `!story update`.\n\n"
        "  mode='replace_body': replace the ENTIRE doc body with "
        "`new_content`. Use ONLY for `!story update`. Atomic in "
        "Drive -- the previous body is preserved as a Drive revision "
        "the user can roll back to.\n\n"
        "  mode='replace_match': find a unique snippet `match_text` "
        "in the body and replace it with `replacement_text`. The "
        "tool refuses to call replaceAllText unless `match_text` "
        "occurs EXACTLY ONCE in the doc. Errors are "
        "`revise_match_not_found` (0 occurrences) or "
        "`revise_match_ambiguous` (2+ occurrences) -- include more "
        "surrounding context in `match_text` to make it unique. "
        "Substring matches count: 'cat' inside 'category' will "
        "trigger ambiguity, which is the safe behaviour.\n\n"
        "After a successful revise the local word_count is "
        "recomputed by re-reading the doc (no drift) and the cached "
        "revision_id is refreshed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "story_key": {
                "type": "string",
                "description": "Existing story alias.",
            },
            "operation_intent": {
                "type": "string",
                "enum": ["targeted_revise", "broad_update"],
                "description": (
                    "MUST match the user's Discord trigger: pass "
                    "'targeted_revise' for `!story revise <alias> "
                    "<instruction>`, 'broad_update' for `!story "
                    "update <alias> <instruction>`. The handler "
                    "rejects (intent, mode) combinations other than "
                    "(targeted_revise, replace_match) and "
                    "(broad_update, replace_body)."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["replace_body", "replace_match"],
                "description": (
                    "replace_body: full rewrite, requires `new_content`. "
                    "Only legal with operation_intent='broad_update'.\n"
                    "replace_match: anchor-based edit, requires "
                    "`match_text` + `replacement_text`. Only legal "
                    "with operation_intent='targeted_revise'."
                ),
            },
            "new_content": {
                "type": "string",
                "description": (
                    "For mode='replace_body': the new entire body. "
                    "Plain text only at v1 (Markdown is NOT rendered)."
                ),
            },
            "match_text": {
                "type": "string",
                "description": (
                    "For mode='replace_match': the snippet to find. "
                    "Must occur exactly once in the doc body. Include "
                    "enough surrounding context to be unique."
                ),
            },
            "replacement_text": {
                "type": "string",
                "description": (
                    "For mode='replace_match': the text to substitute "
                    "in. Empty string is allowed (deletes the match)."
                ),
            },
        },
        "required": ["story_key", "operation_intent", "mode"],
        "additionalProperties": False,
    },
}


# The only two legal (operation_intent, mode) pairings. Anything else
# is a structural mismatch -- see Motoko's PR #4 smoke-test failure
# where the model bypassed `revise_match_ambiguous` by silently
# switching from mode=replace_match to mode=replace_body inside the
# same tool call. The intent/mode coupling makes that bypass an
# explicit lie about the user's trigger rather than a quiet fallback.
_LEGAL_INTENT_MODE_PAIRS: Dict[str, str] = {
    "targeted_revise": "replace_match",
    "broad_update":    "replace_body",
}


def handle_story_doc_revise(args: dict, **_kwargs) -> str:
    raw_key = args.get("story_key")
    story_key, alias_err = validate_story_key(raw_key)
    if alias_err:
        return _struct_error("invalid_story_key", alias_err)

    operation_intent = (args.get("operation_intent") or "").strip().lower()
    if operation_intent not in _LEGAL_INTENT_MODE_PAIRS:
        return _struct_error(
            "invalid_operation_intent",
            "operation_intent must be 'targeted_revise' (for `!story "
            "revise`) or 'broad_update' (for `!story update`); got "
            f"{args.get('operation_intent')!r}. The intent must match "
            "the user's Discord trigger.",
            received=args.get("operation_intent"),
        )

    mode = (args.get("mode") or "").strip().lower()
    if mode not in ("replace_body", "replace_match"):
        return _struct_error(
            "invalid_mode",
            f"mode must be 'replace_body' or 'replace_match', got "
            f"{args.get('mode')!r}",
        )

    # Intent-mode coupling guard. Two legal pairings only:
    #   (targeted_revise, replace_match) and (broad_update, replace_body).
    # Anything else is a structural mismatch -- typically the model
    # trying to route around revise_match_ambiguous by switching modes
    # mid-tool-call (the exact failure Motoko surfaced in PR #4 smoke
    # test). Reject before any auth check or Docs API call.
    expected_mode = _LEGAL_INTENT_MODE_PAIRS[operation_intent]
    if mode != expected_mode:
        if operation_intent == "targeted_revise":
            recovery = (
                "If `mode=replace_match` returned `revise_match_ambiguous` "
                "or `revise_match_not_found`, surface that error to the "
                "user verbatim -- DO NOT switch to mode=replace_body or "
                "operation_intent=broad_update to bypass the guard. The "
                "user explicitly asked for a targeted edit by typing "
                "`!story revise`. If the user actually wants a full "
                "rewrite, they should re-issue the command as "
                "`!story update`."
            )
        else:  # broad_update
            recovery = (
                "Broad updates always rewrite the whole body. If you "
                "want a targeted edit, the user should have typed "
                "`!story revise` (operation_intent='targeted_revise', "
                "mode='replace_match'); ask them to re-issue the "
                "command if you misread the trigger."
            )
        return _struct_error(
            "intent_mode_mismatch",
            f"operation_intent='{operation_intent}' requires "
            f"mode='{expected_mode}', got mode='{mode}'. {recovery}",
            operation_intent=operation_intent,
            mode=mode,
            expected_mode=expected_mode,
        )

    # Mode-specific argument validation. Done in code rather than via
    # JSON Schema oneOf because chat-tuned models handle conditional
    # required fields inconsistently and we want clearer error messages.
    if mode == "replace_body":
        new_content = args.get("new_content")
        if not isinstance(new_content, str):
            return _struct_error(
                "invalid_new_content",
                f"mode='replace_body' requires `new_content` (string), "
                f"got {type(new_content).__name__}",
            )
        # Empty new_content IS allowed -- it clears the doc body
        # (subsequent appends would start fresh). The agent should
        # rarely do this, but we don't reject it.
    else:  # replace_match
        match_text = args.get("match_text")
        if not isinstance(match_text, str) or not match_text:
            return _struct_error(
                "invalid_match_text",
                "mode='replace_match' requires a non-empty `match_text`",
            )
        replacement_text = args.get("replacement_text")
        if not isinstance(replacement_text, str):
            return _struct_error(
                "invalid_replacement_text",
                f"mode='replace_match' requires `replacement_text` (string), "
                f"got {type(replacement_text).__name__}",
            )

    if (auth_err := _auth_check()) is not None:
        return auth_err

    row, lookup_err = _resolve_story_or_error(story_key)
    if lookup_err:
        return lookup_err

    doc_id = row["doc_id"]
    client = GoogleDocsClient()

    try:
        if mode == "replace_body":
            client.replace_body(doc_id, args["new_content"])
        else:
            client.replace_match(
                doc_id,
                args["match_text"],
                args["replacement_text"],
            )
    except GoogleDocsError as exc:
        # Map the precise unique-match-validation messages to structured
        # error keys the model can branch on. The handler -- not the
        # client -- owns the user-facing hint, because:
        #   1. The client's error string is for logs (terse, may be
        #      shorter than what the model needs).
        #   2. Recovery guidance has to name the recovery commands
        #      (`story_doc_read`, `mode=replace_body`) and that
        #      knowledge belongs to the tool layer, not the HTTP wrapper.
        # Anything that isn't one of the structured cases is a generic
        # Docs API error and we DO pass the client's message through.
        msg = str(exc)
        if msg.startswith("match_text not found"):
            return _struct_error(
                "revise_match_not_found",
                "match_text was not found in the document body. Use a "
                "different snippet from `story_doc_read` output, add "
                "more surrounding context to make the snippet match, "
                "or use mode=replace_body for a broader rewrite.",
                story_key=story_key,
                doc_id=doc_id,
                match_text=args.get("match_text"),
            )
        if msg.startswith("match_text ambiguous"):
            # Pull the count out of "(N occurrences)". A regex is needed
            # because str.split tokens like "(3" don't pass isdigit().
            count_match = re.search(r"\((\d+)\s+occurrence", msg)
            count = int(count_match.group(1)) if count_match else None
            count_phrase = (
                f"{count} occurrences" if count is not None
                else "multiple occurrences"
            )
            return _struct_error(
                "revise_match_ambiguous",
                f"match_text matched {count_phrase} in the document. "
                "Add more surrounding context to make the snippet "
                "unique (e.g., include the words just before and after "
                "it), or use mode=replace_body for a broader rewrite.",
                story_key=story_key,
                doc_id=doc_id,
                match_text=args.get("match_text"),
                occurrences=count,
            )
        return _struct_error(
            "google_docs_error",
            msg,
            status_code=exc.status_code,
        )

    # Recompute word count by re-reading the body. More API cost than a
    # delta calculation but always accurate -- revise is infrequent and
    # we'd rather report a correct number than a fast wrong one.
    try:
        new_body = client.read_doc_text(doc_id)
        new_word_count = _word_count(new_body)
    except GoogleDocsError as exc:
        # The revise itself succeeded (Drive saved a revision). The
        # follow-up read failed -- that's a soft warning, not a fatal
        # error. Report the revise as successful but mark the count as
        # unknown so the caller knows the cached value is stale.
        logger.warning(
            "revise post-read failed for %s: %s", story_key, exc
        )
        new_word_count = None

    revision_id = client.get_latest_revision_id(doc_id)

    store = StoryDocStore()
    try:
        # Word count: stomp the cached value with the recounted total.
        # We can't use bump_word_count here because revise can shrink
        # OR grow the doc; absolute set is correct.
        if new_word_count is not None:
            try:
                store.upsert_story(
                    story_key=story_key,
                    doc_id=doc_id,
                    word_count=new_word_count,
                    last_revision_id=revision_id,
                    mode="update",
                )
            except StoryDocError as exc:
                logger.warning(
                    "post-revise store update failed for %s: %s",
                    story_key, exc,
                )
        else:
            # Couldn't recount -- still refresh revision_id so the
            # cached one isn't lying about which version we're on.
            try:
                store.upsert_story(
                    story_key=story_key,
                    doc_id=doc_id,
                    last_revision_id=revision_id,
                    mode="update",
                )
            except StoryDocError as exc:
                logger.warning(
                    "post-revise revision-only update failed for %s: %s",
                    story_key, exc,
                )
    finally:
        store.close()

    body: Dict[str, Any] = {
        "story_key": story_key,
        "doc_id": doc_id,
        "url": client.web_url(doc_id),
        "title": row.get("title"),
        "mode": mode,
        "revision_id": revision_id,
    }
    if new_word_count is not None:
        body["word_count"] = new_word_count
    else:
        body["word_count"] = None
        body["word_count_stale"] = True
    return tool_result(body)


# ===========================================================================
# Shared helpers
# ===========================================================================

def _resolve_story_or_error(story_key: str):
    """Look up ``story_key`` in the store.

    Returns ``(row_dict, None)`` on success; ``(None, error_json)`` if
    the alias does not exist. The error JSON is the standard
    ``story_key_not_found`` shape so all read/append/status handlers
    surface the same actionable hint.
    """
    store = StoryDocStore()
    try:
        row = store.get_story(story_key)
    finally:
        store.close()
    if row is None:
        return None, _struct_error(
            "story_key_not_found",
            f"No story with alias '{story_key}' exists. "
            "Run `!story list` (or `hermes story list`) to see existing "
            "aliases, or `!story start <alias> <prompt>` to create a "
            "new one.",
            story_key=story_key,
        )
    return row, None


def _row_to_status(story_key: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Project the SQLite row into the public status payload shape."""
    return {
        "story_key": story_key,
        "doc_id": row.get("doc_id"),
        "title": row.get("title"),
        "word_count": int(row.get("word_count") or 0),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_revision_id": row.get("last_revision_id"),
        "summary": row.get("summary"),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _word_count(text: str) -> int:
    """Naive whitespace-split word count. Good enough for status display."""
    if not text:
        return 0
    return len(text.split())
