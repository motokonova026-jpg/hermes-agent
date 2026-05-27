"""CLI surface for the story_doc plugin.

PR #2 adds ``hermes story setup`` (mirrors the workspace skill's
setup.py CLI: ``--check`` / ``--client-secret PATH`` / ``--auth-url`` /
``--auth-code CODE`` / ``--revoke``) so the model and the user can
bootstrap OAuth from any platform (CLI, Telegram, Discord) without
needing a browser on the box where Hermes runs.

PR #1's ``hermes story list`` is unchanged.

Mirrors the argparse style used by ``plugins/google_meet/cli.py``:
``register_cli(subparser)`` builds the tree, ``story_command(args)``
dispatches.
"""

from __future__ import annotations

import argparse
import json
import sys

from plugins.story_doc.store import StoryDocStore


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes story`` argparse tree.

    Called by the plugin loader at startup. Subparser ``dest`` is
    ``story_action`` (mirrors google_meet's per-plugin dest naming so
    different plugins' args don't collide on ``args.command``).
    """
    subs = subparser.add_subparsers(dest="story_action")

    # --- list -----------------------------------------------------------
    list_p = subs.add_parser(
        "list",
        help="List all known story aliases in this profile.",
        description=(
            "Dump the contents of the story_doc SQLite store. "
            "Aliases that have never had `story_doc_create` called on "
            "them won't appear here."
        ),
    )
    list_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON (machine-readable) instead of human-friendly output.",
    )
    list_p.set_defaults(_handler=_handle_list)

    # --- setup ----------------------------------------------------------
    setup_p = subs.add_parser(
        "setup",
        help="Set up or inspect Google OAuth for the story_doc plugin.",
        description=(
            "Bootstrap Google OAuth so story_doc tools can write to your "
            "Google Drive. Mirrors `python google-workspace/scripts/setup.py` "
            "so the muscle memory transfers. Token is stored at "
            "$HERMES_HOME/story_doc/google_token.json (per-profile). "
            "client_secret.json is shared with the google-workspace "
            "skill at $HERMES_HOME/google_client_secret.json by "
            "default; override with STORY_DOC_GOOGLE_CLIENT_SECRET."
        ),
    )
    setup_group = setup_p.add_mutually_exclusive_group(required=True)
    setup_group.add_argument(
        "--check",
        action="store_true",
        help="Print auth status and exit 0 if ready, 1 otherwise.",
    )
    setup_group.add_argument(
        "--client-secret",
        metavar="PATH",
        help=(
            "Copy a downloaded client_secret.json into the resolved "
            "location. Pass the path you downloaded from "
            "https://console.cloud.google.com/apis/credentials "
            "(OAuth client of type 'Desktop app')."
        ),
    )
    setup_group.add_argument(
        "--auth-url",
        action="store_true",
        help=(
            "Print the OAuth consent URL. Open it in any browser, "
            "complete consent; you'll be redirected to a "
            "'can't-connect' page on http://localhost:1/ -- copy that "
            "full URL (it has ?code=...&state=...) and pass it to "
            "--auth-code."
        ),
    )
    setup_group.add_argument(
        "--auth-code",
        metavar="CODE_OR_URL",
        help=(
            "Exchange a consent code for tokens. Accepts either the bare "
            "auth code or the full http://localhost:1/?code=... redirect "
            "URL."
        ),
    )
    setup_group.add_argument(
        "--revoke",
        action="store_true",
        help="Revoke the stored token and delete the local token file.",
    )
    setup_p.set_defaults(_handler=_handle_setup)

    subparser.set_defaults(func=story_command)


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------

def story_command(args: argparse.Namespace) -> int:
    """Top-level dispatch for ``hermes story ...``.

    Returns a process exit code: 0 on success, 1 on action error,
    2 on usage error.
    """
    handler = getattr(args, "_handler", None)
    if handler is None:
        print(
            "usage: hermes story {list,setup} [...]\n"
            "  hermes story list   -- list known aliases\n"
            "  hermes story setup  -- configure Google OAuth "
            "(--check / --client-secret / --auth-url / --auth-code / --revoke)",
            file=sys.stderr,
        )
        return 2
    return handler(args)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_list(args: argparse.Namespace) -> int:
    store = StoryDocStore()
    try:
        rows = store.list_stories()
    finally:
        store.close()

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    if not rows:
        print(
            "No stories yet. Use `!story start <alias> <prompt>` from a "
            "Discord channel where Momo is active to create one."
        )
        return 0

    print(f"{'ALIAS':<32}  {'WORDS':>6}  TITLE")
    print(f"{'-' * 32}  {'-' * 6}  {'-' * 40}")
    for r in rows:
        alias = str(r.get("story_key") or "")
        wc = int(r.get("word_count") or 0)
        title = (r.get("title") or "(untitled)")[:60]
        print(f"{alias:<32}  {wc:>6}  {title}")
    return 0


def _handle_setup(args: argparse.Namespace) -> int:
    """Dispatch the various ``setup --...`` flags.

    Each branch translates argparse args into a call against
    ``plugins.story_doc.oauth`` and prints user-facing output (the
    actual logic lives in oauth.py so it's testable in isolation).
    """
    # Lazy import: keeps `hermes story list` from paying the import cost
    # for google_auth_oauthlib (which oauth.py imports lazily anyway,
    # but defence in depth keeps `--help` snappy).
    from plugins.story_doc import oauth as _oauth

    if args.check:
        status = _oauth.check_status()
        if status["ok"]:
            print(f"AUTHENTICATED: {status['message']}")
            print(f"Token: {_oauth.token_path()}")
            return 0
        print(f"NOT_AUTHENTICATED ({status['state']}): {status['message']}")
        if status["state"] == "missing_scopes":
            for s in status.get("missing_scopes", []):
                print(f"  missing scope: {s}")
        return 1

    if args.client_secret is not None:
        try:
            dest = _oauth.store_client_secret(args.client_secret)
        except _oauth.OAuthError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"OK: client_secret saved to {dest}")
        return 0

    if args.auth_url:
        try:
            url = _oauth.get_auth_url()
        except _oauth.OAuthError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        # Print the URL on its own line so the agent / pipe consumers can
        # extract it cleanly.
        print(url)
        return 0

    if args.auth_code is not None:
        try:
            dest = _oauth.exchange_auth_code(args.auth_code)
        except _oauth.OAuthError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"OK: token saved to {dest}")
        return 0

    if args.revoke:
        _oauth.revoke()
        print("OK: token revoked and local files removed.")
        return 0

    # argparse's mutually_exclusive_group(required=True) prevents us from
    # reaching here, but keep a clean fallback just in case.
    print("usage: hermes story setup --check | --client-secret PATH | "
          "--auth-url | --auth-code CODE | --revoke",
          file=sys.stderr)
    return 2
