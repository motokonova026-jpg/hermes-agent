"""Thin Google Docs + Drive client used by the story_doc tools.

PR #2 surface (the minimum to support ``story_doc_create``):

  * ``GoogleDocsClient.create_doc(title)`` -- create an empty Google Doc,
    return its document_id.
  * ``GoogleDocsClient.insert_text_at_end(doc_id, text)`` -- batchUpdate
    insertText at endIndex-1; returns the number of characters inserted.
  * ``GoogleDocsClient.get_latest_revision_id(doc_id)`` -- ask Drive for
    the most recent revision id (used by status/checkpointing).
  * ``GoogleDocsClient.web_url(doc_id)`` -- canonical user-facing URL.

The client wraps ``googleapiclient.discovery.build`` for both the Docs
v1 and Drive v3 services. It does NOT do its own token storage -- it
takes ``credentials`` from ``oauth.get_credentials()`` and lets that
module own the refresh path.

The 401 -> refresh -> retry loop mirrors ``SpotifyClient.request`` from
``plugins/spotify/client.py``: googleapiclient raises ``HttpError(401)``
on expired tokens, we catch once, call ``oauth.get_credentials()``
again (which refreshes the token in-place + rewrites the token file),
rebuild the service with fresh creds, and replay the call. A second
401 surfaces to the caller as a ``GoogleDocsError``.

PR #3+ will add ``read_doc``, ``append`` (likely just an alias for
``insert_text_at_end`` if no separator munging is needed), and
``revise`` (deleteContentRange + insertText OR a hardened
``replaceAllText`` with unique-match validation per Rob's PR #2
guardrail).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from plugins.story_doc import oauth as _oauth

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GoogleDocsError(RuntimeError):
    """Raised by GoogleDocsClient for user-actionable Docs/Drive failures."""

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class GoogleDocsClient:
    """Lightweight Docs + Drive helper.

    Constructed lazily: services are built on first use so that just
    importing the module (and exercising it in tests) doesn't pull in
    googleapiclient unless we actually need it.
    """

    def __init__(self, credentials=None) -> None:
        # If credentials weren't passed in, fetch them lazily on first use
        # so that constructing a client never triggers an OAuth check.
        self._credentials = credentials
        self._docs_service: Any = None
        self._drive_service: Any = None

    # ------------------------------------------------------------------
    # Service lifecycle
    # ------------------------------------------------------------------

    def _ensure_credentials(self):
        if self._credentials is None:
            # OAuthError bubbles up if not authed; the tool handler
            # catches it and converts to a tool_error JSON.
            self._credentials = _oauth.get_credentials()
        return self._credentials

    def _docs(self):
        if self._docs_service is None:
            from googleapiclient.discovery import build  # lazy
            self._docs_service = build(
                "docs", "v1",
                credentials=self._ensure_credentials(),
                cache_discovery=False,
            )
        return self._docs_service

    def _drive(self):
        if self._drive_service is None:
            from googleapiclient.discovery import build  # lazy
            self._drive_service = build(
                "drive", "v3",
                credentials=self._ensure_credentials(),
                cache_discovery=False,
            )
        return self._drive_service

    def _reset_services_with_fresh_creds(self):
        """Drop cached services so the next call rebuilds with refreshed creds."""
        self._credentials = None
        self._docs_service = None
        self._drive_service = None

    def _is_unauth_error(self, exc: Exception) -> bool:
        """Detect googleapiclient.errors.HttpError(401) without importing
        the class at module-import time."""
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 401:
            return True
        # Some versions surface the status differently
        return getattr(exc, "status_code", None) == 401

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def create_doc(self, title: str) -> str:
        """Create an empty Google Doc named ``title``; return its document_id.

        Raises ``GoogleDocsError`` on failure.
        """
        if not title or not isinstance(title, str):
            raise GoogleDocsError("title is required and must be a non-empty string")

        def _call():
            return (
                self._docs()
                .documents()
                .create(body={"title": title})
                .execute()
            )

        try:
            doc = self._call_with_refresh(_call)
        except Exception as exc:
            raise self._friendly_error(exc, action="create document") from exc

        doc_id = doc.get("documentId")
        if not doc_id:
            raise GoogleDocsError(
                f"Docs API returned no documentId; response keys: "
                f"{sorted(doc.keys()) if isinstance(doc, dict) else type(doc).__name__}"
            )
        return doc_id

    def read_doc_text(self, doc_id: str) -> str:
        """Return the plain-text body of ``doc_id``.

        Walks ``body.content[].paragraph.elements[].textRun.content`` and
        joins the runs in document order, mirroring the workspace skill's
        ``_extract_doc_text`` helper at
        ``skills/productivity/google-workspace/scripts/google_api.py``.
        Tables, table-of-contents entries, and section breaks contribute
        no text in this v1 -- story_doc bodies are plain-text appends, so
        callers will only see what was inserted.

        Returns the empty string for an empty doc (newly-created docs
        have a trailing newline that we strip when computing word counts
        downstream).
        """
        def _get():
            return (
                self._docs()
                .documents()
                .get(documentId=doc_id)
                .execute()
            )

        try:
            doc = self._call_with_refresh(_get)
        except Exception as exc:
            raise self._friendly_error(exc, action="read document body") from exc

        return _extract_plain_text(doc)

    def insert_text_at_end(self, doc_id: str, text: str) -> int:
        """Append ``text`` to the end of ``doc_id``. Returns char count inserted.

        Uses Docs ``batchUpdate`` with ``insertText`` at the document's
        current ``endIndex - 1`` (the last index is reserved for the
        implicit trailing newline; insertion must precede it).
        """
        if not isinstance(text, str):
            raise GoogleDocsError(
                f"text must be a string, got {type(text).__name__}"
            )
        if text == "":
            return 0  # no-op, but don't blow up the caller

        # Find the current end of the document.
        def _get():
            return (
                self._docs()
                .documents()
                .get(documentId=doc_id, fields="body.content(endIndex)")
                .execute()
            )

        try:
            doc = self._call_with_refresh(_get)
        except Exception as exc:
            raise self._friendly_error(exc, action="read document end index") from exc

        end_index = _last_end_index(doc)
        if end_index is None or end_index < 1:
            # A freshly-created doc has body.content with at least one
            # element whose endIndex is >= 2 (1 char start + trailing
            # newline). Defensive fallback: insert at index 1.
            end_index = 2

        insert_at = max(1, end_index - 1)

        def _call():
            return (
                self._docs()
                .documents()
                .batchUpdate(
                    documentId=doc_id,
                    body={
                        "requests": [{
                            "insertText": {
                                "location": {"index": insert_at},
                                "text": text,
                            }
                        }]
                    },
                )
                .execute()
            )

        try:
            self._call_with_refresh(_call)
        except Exception as exc:
            raise self._friendly_error(exc, action="insert text") from exc

        return len(text)

    def replace_body(self, doc_id: str, new_text: str) -> int:
        """Atomically replace the entire body of ``doc_id`` with ``new_text``.

        Single ``batchUpdate`` containing two requests: a
        ``deleteContentRange`` covering everything from index 1 up to the
        document's current ``endIndex - 1`` (the trailing implicit newline
        is preserved), then an ``insertText`` at index 1 with the new
        body. Atomic: if either request fails, neither is applied and the
        Drive API rolls back without producing a revision.

        Returns the number of characters inserted (i.e., ``len(new_text)``).
        Empty ``new_text`` is allowed -- it just clears the body.
        """
        if not isinstance(new_text, str):
            raise GoogleDocsError(
                f"new_text must be a string, got {type(new_text).__name__}"
            )

        def _get():
            return (
                self._docs()
                .documents()
                .get(documentId=doc_id, fields="body.content(endIndex)")
                .execute()
            )

        try:
            doc = self._call_with_refresh(_get)
        except Exception as exc:
            raise self._friendly_error(exc, action="read document end index") from exc

        end_index = _last_end_index(doc)
        if end_index is None or end_index <= 2:
            # Doc is effectively empty (just the trailing newline).
            # Skip the deleteContentRange (Docs rejects empty deletes)
            # and just insert. If new_text is also empty, this is a no-op.
            if not new_text:
                return 0
            return self.insert_text_at_end(doc_id, new_text)

        requests: list = [{
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": end_index - 1},
            }
        }]
        if new_text:
            requests.append({
                "insertText": {
                    "location": {"index": 1},
                    "text": new_text,
                }
            })

        def _call():
            return (
                self._docs()
                .documents()
                .batchUpdate(
                    documentId=doc_id,
                    body={"requests": requests},
                )
                .execute()
            )

        try:
            self._call_with_refresh(_call)
        except Exception as exc:
            raise self._friendly_error(exc, action="replace document body") from exc

        return len(new_text)

    def replace_match(
        self,
        doc_id: str,
        match_text: str,
        replacement_text: str,
    ) -> int:
        """Replace ``match_text`` with ``replacement_text`` if and only if
        ``match_text`` occurs **exactly once** in the doc.

        Returns 1 on success. Raises ``GoogleDocsError`` with a structured
        message in two precise cases (caller maps these to the
        ``revise_match_not_found`` / ``revise_match_ambiguous`` tool_error
        keys):

        * ``match_text`` doesn't appear in the doc body -> message starts
          with "match_text not found".
        * ``match_text`` appears 2+ times -> message starts with
          "match_text ambiguous (N occurrences)".

        The validation uses the literal-substring count from the
        freshly-fetched body. ``replaceAllText`` with ``containsText.text``
        also matches by literal substring (default ``matchCase=true``), so
        the count and the API agree.

        We intentionally don't expose ``replaceAllText`` without the
        unique-match guard -- per Rob's PR #2 guardrail, no risky global
        replacements.
        """
        if not isinstance(match_text, str) or not match_text:
            raise GoogleDocsError("match_text must be a non-empty string")
        if not isinstance(replacement_text, str):
            raise GoogleDocsError(
                f"replacement_text must be a string, "
                f"got {type(replacement_text).__name__}"
            )

        # Read first so we can validate uniqueness BEFORE calling
        # replaceAllText. Yes, this is a TOCTOU window -- if someone
        # edits the doc in Drive between the read and the replace, the
        # count could change. Acceptable for v1: the worst case is a
        # different number of replacements than the user expected, which
        # they can roll back via Drive's revision history.
        body = self.read_doc_text(doc_id)
        count = body.count(match_text)

        if count == 0:
            raise GoogleDocsError(
                f"match_text not found in document body. "
                f"Try a different snippet, or use mode=replace_body for "
                f"a full rewrite."
            )
        if count > 1:
            raise GoogleDocsError(
                f"match_text ambiguous ({count} occurrences). "
                f"Include more surrounding context to make the match "
                f"unique, or split into multiple revise calls."
            )

        # Exactly one match -- safe to call replaceAllText.
        def _call():
            return (
                self._docs()
                .documents()
                .batchUpdate(
                    documentId=doc_id,
                    body={"requests": [{
                        "replaceAllText": {
                            "containsText": {
                                "text": match_text,
                                "matchCase": True,
                            },
                            "replaceText": replacement_text,
                        }
                    }]},
                )
                .execute()
            )

        try:
            self._call_with_refresh(_call)
        except Exception as exc:
            raise self._friendly_error(exc, action="replace matched text") from exc

        return 1

    def get_latest_revision_id(self, doc_id: str) -> Optional[str]:
        """Return the most recent Drive revision id for ``doc_id``, or None."""
        def _call():
            return (
                self._drive()
                .revisions()
                .list(
                    fileId=doc_id,
                    fields="revisions(id,modifiedTime)",
                    pageSize=1000,  # docs typically have few revisions
                )
                .execute()
            )

        try:
            resp = self._call_with_refresh(_call)
        except Exception as exc:
            # Don't fail the calling tool just because we couldn't read
            # the revision id -- it's informational, not load-bearing.
            logger.warning(
                "failed to fetch revisions for doc %s: %s", doc_id, exc
            )
            return None

        revisions = resp.get("revisions") or []
        if not revisions:
            return None
        # Drive returns revisions in chronological order; last one is latest.
        return revisions[-1].get("id")

    @staticmethod
    def web_url(doc_id: str) -> str:
        """Canonical user-facing URL for a Google Doc."""
        return f"https://docs.google.com/document/d/{doc_id}/edit"

    # ------------------------------------------------------------------
    # 401 -> refresh -> retry
    # ------------------------------------------------------------------

    def _call_with_refresh(self, fn):
        """Execute ``fn()``; on a single 401, refresh creds and retry once."""
        try:
            return fn()
        except Exception as exc:
            if not self._is_unauth_error(exc):
                raise
            logger.info(
                "Google API returned 401; refreshing credentials and retrying"
            )
            self._reset_services_with_fresh_creds()
            return fn()

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------

    def _friendly_error(self, exc: Exception, *, action: str) -> GoogleDocsError:
        """Wrap a googleapiclient HttpError (or anything else) into a
        GoogleDocsError with an actionable message."""
        # Don't import googleapiclient.errors at module level -- the lazy
        # access keeps tests light.
        status = getattr(getattr(exc, "resp", None), "status", None)
        detail = ""
        try:
            content = getattr(exc, "content", b"")
            if isinstance(content, (bytes, bytearray)):
                detail = content.decode("utf-8", errors="replace")
            elif isinstance(content, str):
                detail = content
        except Exception:
            pass

        if status == 401:
            return GoogleDocsError(
                f"Google rejected the request (401) while trying to {action}. "
                "Token may have been revoked. Run "
                "`hermes story setup --revoke` then `--auth-url` to re-auth.",
                status_code=401,
            )
        if status == 403:
            return GoogleDocsError(
                f"Google denied permission (403) while trying to {action}. "
                "Either the token is missing required scopes, or the "
                "Docs/Drive API is disabled on the GCP project. "
                "Re-run `hermes story setup --auth-url` and re-consent.",
                status_code=403,
            )
        if status == 404:
            return GoogleDocsError(
                f"Google could not find the resource (404) while trying to {action}. "
                "The document may have been deleted or the doc_id is wrong.",
                status_code=404,
            )
        if status == 429:
            return GoogleDocsError(
                f"Google rate-limited the request (429) while trying to {action}. "
                "Wait a moment and try again.",
                status_code=429,
            )
        if status and status >= 500:
            return GoogleDocsError(
                f"Google service error ({status}) while trying to {action}. "
                "This is usually transient; try again in a moment.",
                status_code=status,
            )
        # Unknown failure mode -- surface the type + detail.
        msg = f"Failed to {action}: {type(exc).__name__}: {exc}"
        if detail and detail not in msg:
            # Truncate huge HTML error bodies to keep tool output readable.
            msg += f" | detail: {detail[:300]}"
        return GoogleDocsError(msg, status_code=status)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _last_end_index(doc: Dict[str, Any]) -> Optional[int]:
    """Find the largest endIndex in body.content -- where the doc 'ends'."""
    content = (doc.get("body") or {}).get("content") or []
    end_indices = [
        elt.get("endIndex")
        for elt in content
        if isinstance(elt, dict) and elt.get("endIndex") is not None
    ]
    if not end_indices:
        return None
    return max(int(i) for i in end_indices)


def _extract_plain_text(doc: Dict[str, Any]) -> str:
    """Walk a Docs ``documents.get`` response, return the body text.

    Mirrors ``_extract_doc_text`` in
    ``skills/productivity/google-workspace/scripts/google_api.py``.
    Skips non-paragraph elements (tables, sectionBreak, etc.) -- v1
    story bodies are plain text inserts.
    """
    parts: list[str] = []
    content = (doc.get("body") or {}).get("content") or []
    for element in content:
        if not isinstance(element, dict):
            continue
        paragraph = element.get("paragraph")
        if not isinstance(paragraph, dict):
            continue
        for pe in paragraph.get("elements") or []:
            if not isinstance(pe, dict):
                continue
            text_run = pe.get("textRun")
            if not isinstance(text_run, dict):
                continue
            text = text_run.get("content")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)
