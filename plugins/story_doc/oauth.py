"""Google OAuth helper for the story_doc plugin.

Mirrors ``skills/productivity/google-workspace/scripts/setup.py`` so the
agent UX (``--check`` / ``--client-secret`` / ``--auth-url`` /
``--auth-code``) is identical: the agent already knows how to drive that
flow on CLI / Telegram / Discord, and we don't want a second muscle
memory for the same OAuth dance.

Deltas vs. the workspace skill:

* Token at ``get_hermes_home() / "story_doc" / "google_token.json"``
  instead of the shared ``google_token.json`` -- so story_doc has its
  own narrowly-scoped credentials and never accidentally exfiltrates
  the broader Workspace token.
* Scopes: only ``documents`` + ``drive.file``. No mail, calendar,
  contacts, broad drive, or sheets.
* Client secret reused from the workspace skill's path (``get_hermes_home()
  / "google_client_secret.json"``) by default, so Rob doesn't need a
  second GCP OAuth client. Override via ``STORY_DOC_GOOGLE_CLIENT_SECRET``.
* Pure module: no argparse / __main__ block here. The CLI surface
  (``hermes story setup ...``) lives in ``cli.py`` and calls into these
  functions, so we can unit-test them without subprocess plumbing.

Headless-friendly by construction: redirect URI is ``http://localhost:1``
and we ask the user to paste the redirect URL (the server never has to
listen). This is the same pattern the workspace skill uses; verified
post-removal of ``InstalledAppFlow.run_console`` from
``google-auth-oauthlib`` (see v2 plan, section 1).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Story_doc needs Docs read+write on docs we own, plus the *narrowest* Drive
# scope that lets us list revisions on those docs (drive.file = only files
# this app created or the user explicitly shared with this app -- not the
# user's whole Drive). Adding scopes here is a breaking change for users
# who've already consented; bump and re-run consent if you do.
_SCOPES: List[str] = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

_CLIENT_SECRET_ENV = "STORY_DOC_GOOGLE_CLIENT_SECRET"

# Same redirect target as the workspace skill: a port that nothing is
# listening on, so the browser ends up on a "can't connect" page. The
# *URL bar* still shows ``?code=...&state=...&scope=...`` -- the user
# copies that URL and pastes it into ``--auth-code``.
_REDIRECT_URI = "http://localhost:1"

# Token-refresh leeway: refresh proactively when the token has less than
# this many seconds of validity left. Keeps the Docs API request path
# from racing the expiry.
_REFRESH_LEEWAY_SECONDS = 60


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class OAuthError(RuntimeError):
    """Raised when the OAuth flow fails in a way the user can act on."""


# ---------------------------------------------------------------------------
# Path helpers (computed lazily so tests can monkeypatch HERMES_HOME)
# ---------------------------------------------------------------------------

def token_path() -> Path:
    """Return the profile-scoped story_doc token path."""
    return get_hermes_home() / "story_doc" / "google_token.json"


def client_secret_path() -> Path:
    """Return the active client_secret.json path.

    Order of precedence:

    1. ``STORY_DOC_GOOGLE_CLIENT_SECRET`` env var (explicit override).
    2. ``get_hermes_home() / "google_client_secret.json"`` -- the same
       location the workspace skill uses, so a user who's already set up
       google-workspace doesn't need to download a second file.
    """
    override = os.getenv(_CLIENT_SECRET_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return get_hermes_home() / "google_client_secret.json"


def pending_auth_path() -> Path:
    """Return where get_auth_url stashes PKCE state for exchange_auth_code."""
    return get_hermes_home() / "story_doc" / "google_oauth_pending.json"


# ---------------------------------------------------------------------------
# Token + state IO helpers
# ---------------------------------------------------------------------------

def _normalize_authorized_user_payload(payload: dict) -> dict:
    """Ensure the payload has ``type=authorized_user`` so google-auth can
    load it via ``Credentials.from_authorized_user_file`` later."""
    normalized = dict(payload)
    if not normalized.get("type"):
        normalized["type"] = "authorized_user"
    return normalized


def _load_token_payload(path: Optional[Path] = None) -> dict:
    p = path or token_path()
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _missing_scopes(payload: dict) -> List[str]:
    """Return any required scopes that aren't in the stored token."""
    raw = payload.get("scopes") or payload.get("scope")
    if not raw:
        return []
    granted = {
        s.strip()
        for s in (raw.split() if isinstance(raw, str) else raw)
        if s.strip()
    }
    return sorted(s for s in _SCOPES if s not in granted)


def _save_pending_auth(*, state: str, code_verifier: str) -> None:
    p = pending_auth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "state": state,
                "code_verifier": code_verifier,
                "redirect_uri": _REDIRECT_URI,
            },
            indent=2,
        )
    )


def _load_pending_auth() -> dict:
    p = pending_auth_path()
    if not p.exists():
        raise OAuthError(
            "no pending OAuth session found; run "
            "`hermes story setup --auth-url` first to start one"
        )
    try:
        data = json.loads(p.read_text())
    except Exception as exc:
        raise OAuthError(
            f"could not read pending OAuth session ({exc}); "
            "re-run `hermes story setup --auth-url` to start fresh"
        ) from exc
    if not data.get("state") or not data.get("code_verifier"):
        raise OAuthError(
            "pending OAuth session is missing PKCE data; "
            "re-run `hermes story setup --auth-url` to start fresh"
        )
    return data


def _extract_code_and_state(code_or_url: str) -> Tuple[str, Optional[str]]:
    """Accept either a raw OAuth code or a full redirect URL pasted by the user."""
    if not code_or_url.startswith("http"):
        return code_or_url, None
    parsed = urlparse(code_or_url)
    params = parse_qs(parsed.query)
    if "code" not in params:
        raise OAuthError(
            "no 'code' parameter in URL; paste either the auth code "
            "from the URL bar or the full redirect URL (which starts "
            "with 'http://localhost:1/?code=...')"
        )
    state = params.get("state", [None])[0]
    return params["code"][0], state


def _extract_scopes_from_url(code_or_url: str) -> Optional[List[str]]:
    """Pull the ``scope=...`` param out of a redirect URL, or None if absent.

    Google returns the scopes the user actually granted -- which can be
    a subset of what we requested if they unchecked a permission. We
    persist whatever they granted so refresh doesn't fail with
    invalid_scope.
    """
    if not code_or_url.startswith("http"):
        return None
    params = parse_qs(urlparse(code_or_url).query)
    raw = (params.get("scope") or [""])[0].strip()
    if not raw:
        return None
    return raw.split()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def has_client_secret() -> bool:
    """Whether the OAuth client_secret.json is present at the resolved path."""
    return client_secret_path().exists()


def has_token() -> bool:
    """Whether a token file exists. Doesn't validate it."""
    return token_path().exists()


def store_client_secret(path: str) -> Path:
    """Validate a client_secret.json file and copy it to the resolved location.

    Returns the destination Path. Raises ``OAuthError`` on failure.
    """
    src = Path(path).expanduser().resolve()
    if not src.exists():
        raise OAuthError(f"file not found: {src}")
    try:
        data = json.loads(src.read_text())
    except json.JSONDecodeError as exc:
        raise OAuthError(f"file is not valid JSON: {exc}") from exc
    if "installed" not in data and "web" not in data:
        raise OAuthError(
            "not a Google OAuth client secret file (missing 'installed' "
            "or 'web' key). Download the correct file from "
            "https://console.cloud.google.com/apis/credentials -- pick "
            "an 'OAuth 2.0 Client ID' of type 'Desktop app'."
        )
    dest = client_secret_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2))
    return dest


def get_auth_url() -> str:
    """Build the OAuth consent URL and stash PKCE state for later exchange.

    The user is expected to open this URL in any browser, complete consent,
    then paste either the full redirect URL or just the ``code=...`` value
    into ``exchange_auth_code()``.
    """
    if not has_client_secret():
        raise OAuthError(
            "no client_secret.json stored; run "
            "`hermes story setup --client-secret /path/to/client_secret.json` "
            "first (or set the STORY_DOC_GOOGLE_CLIENT_SECRET env var)"
        )

    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        str(client_secret_path()),
        scopes=_SCOPES,
        redirect_uri=_REDIRECT_URI,
        autogenerate_code_verifier=True,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    _save_pending_auth(state=state, code_verifier=flow.code_verifier)
    return auth_url


def exchange_auth_code(code_or_url: str) -> Path:
    """Exchange an authorization code for tokens; persist token file.

    Returns the path the token was written to. Raises ``OAuthError`` on
    failure (and leaves the pending-auth file in place so the user can
    retry without re-doing consent in their browser).
    """
    if not has_client_secret():
        raise OAuthError(
            "no client_secret.json stored; run "
            "`hermes story setup --client-secret PATH` first"
        )

    pending = _load_pending_auth()
    code, returned_state = _extract_code_and_state(code_or_url)
    if returned_state and returned_state != pending["state"]:
        raise OAuthError(
            "OAuth state mismatch -- the redirect URL doesn't belong to "
            "this auth session. Run `hermes story setup --auth-url` to "
            "start fresh."
        )

    # Use the scopes the user actually granted (from the redirect URL),
    # not what we requested -- otherwise google-auth fails refresh with
    # invalid_scope when the user deselects a permission.
    granted_scopes = _extract_scopes_from_url(code_or_url) or list(_SCOPES)

    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        str(client_secret_path()),
        scopes=granted_scopes,
        redirect_uri=pending.get("redirect_uri", _REDIRECT_URI),
        state=pending["state"],
        code_verifier=pending["code_verifier"],
    )

    # Allow partial-scope grants (matches the workspace skill behaviour).
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        raise OAuthError(
            f"token exchange failed: {exc}. The code may have expired -- "
            "run `hermes story setup --auth-url` to get a fresh one."
        ) from exc

    creds = flow.credentials
    payload = _normalize_authorized_user_payload(json.loads(creds.to_json()))

    # google-auth's to_json() writes the *requested* scopes, not the
    # granted ones. Overwrite with what the user actually granted so
    # subsequent refreshes don't fail.
    actually_granted = (
        list(creds.granted_scopes)
        if hasattr(creds, "granted_scopes") and creds.granted_scopes
        else []
    )
    if actually_granted:
        payload["scopes"] = actually_granted
    elif granted_scopes != _SCOPES:
        payload["scopes"] = granted_scopes

    dest = token_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2))
    pending_auth_path().unlink(missing_ok=True)
    return dest


def revoke() -> None:
    """Best-effort revoke + delete the local token file."""
    if not has_token():
        return

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = Credentials.from_authorized_user_file(str(token_path()), _SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        import urllib.request
        urllib.request.urlopen(  # noqa: S310 - well-known google endpoint
            urllib.request.Request(
                f"https://oauth2.googleapis.com/revoke?token={creds.token}",
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        )
        logger.info("story_doc OAuth token revoked at oauth2.googleapis.com")
    except Exception as exc:
        # Network failure shouldn't prevent local cleanup. The token may
        # already be invalid on Google's side anyway.
        logger.warning(
            "remote token revocation failed (%s); deleting local file anyway",
            exc,
        )

    token_path().unlink(missing_ok=True)
    pending_auth_path().unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Status check (called by cli `setup --check` and by tool handlers)
# ---------------------------------------------------------------------------

def check_status() -> Dict[str, Any]:
    """Return a structured status describing what (if anything) is missing.

    Shape:
      {
        "ok": bool,
        "state": "ready" | "missing_client_secret" | "missing_token"
                  | "token_invalid" | "missing_scopes" | "refresh_failed",
        "message": "...",
        "missing_scopes": [scope, ...]   # only when state == missing_scopes
      }

    This is the single source of truth for "is story_doc usable right now"
    -- both the CLI and the tool handlers' auth gate funnel through it.
    """
    if not has_client_secret():
        return {
            "ok": False,
            "state": "missing_client_secret",
            "message": (
                "No Google OAuth client_secret.json. Run "
                "`hermes story setup --client-secret /path/to/client_secret.json` "
                "(or set STORY_DOC_GOOGLE_CLIENT_SECRET)."
            ),
        }
    if not has_token():
        return {
            "ok": False,
            "state": "missing_token",
            "message": (
                "No Google token. Run `hermes story setup --auth-url` to "
                "get the consent URL, then `hermes story setup --auth-code "
                "<paste-redirect-url>` to finish."
            ),
        }

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        # Don't pass scopes to from_authorized_user_file -- the user may
        # have authorized a subset, and validating against the full list
        # here triggers a spurious invalid_scope error on refresh.
        creds = Credentials.from_authorized_user_file(str(token_path()))
    except Exception as exc:
        return {
            "ok": False,
            "state": "token_invalid",
            "message": (
                f"Token file is unreadable ({exc}). Run "
                "`hermes story setup --revoke` then `--auth-url` to start over."
            ),
        }

    payload = _load_token_payload()
    missing = _missing_scopes(payload)

    if creds.valid:
        if missing:
            return {
                "ok": False,
                "state": "missing_scopes",
                "missing_scopes": missing,
                "message": (
                    "Token is valid but missing required scopes: "
                    f"{', '.join(missing)}. Re-run "
                    "`hermes story setup --auth-url` and re-consent."
                ),
            }
        return {"ok": True, "state": "ready", "message": "Authenticated."}

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            return {
                "ok": False,
                "state": "refresh_failed",
                "message": (
                    f"Token refresh failed ({exc}). Run "
                    "`hermes story setup --revoke` then `--auth-url` to start over."
                ),
            }
        token_path().write_text(
            json.dumps(
                _normalize_authorized_user_payload(json.loads(creds.to_json())),
                indent=2,
            )
        )
        if missing:
            return {
                "ok": False,
                "state": "missing_scopes",
                "missing_scopes": missing,
                "message": (
                    "Token refreshed but missing required scopes: "
                    f"{', '.join(missing)}. Re-run "
                    "`hermes story setup --auth-url` and re-consent."
                ),
            }
        return {"ok": True, "state": "ready", "message": "Authenticated (refreshed)."}

    return {
        "ok": False,
        "state": "token_invalid",
        "message": (
            "Token has no usable refresh path. Run "
            "`hermes story setup --revoke` then `--auth-url` to start over."
        ),
    }


def get_credentials():
    """Return refreshed Google ``Credentials`` ready for googleapiclient.

    Raises ``OAuthError`` with an actionable message if anything is
    wrong. Callers (the GoogleDocsClient and the story_doc tool
    handlers) should let the error bubble up so the model can read it.
    """
    status = check_status()
    if not status["ok"]:
        raise OAuthError(status["message"])

    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(token_path()))
    if not creds.valid:
        # check_status() refreshed if needed and rewrote the token -- read
        # the fresh one. If it's STILL not valid, something raced; let the
        # caller deal.
        from google.auth.transport.requests import Request
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path().write_text(
                json.dumps(
                    _normalize_authorized_user_payload(json.loads(creds.to_json())),
                    indent=2,
                )
            )
        else:
            raise OAuthError(
                "Credentials are not valid and have no refresh token. "
                "Run `hermes story setup --revoke` then `--auth-url`."
            )
    return creds
