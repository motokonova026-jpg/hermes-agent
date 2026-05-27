"""story_doc plugin -- bundled, opt-in standalone.

PR #4 scope (this release):

* Adds ``story_doc_revise`` with two modes: ``replace_body`` (full
  rewrite) and ``replace_match`` (anchor-based, refused unless the
  anchor is unique). The ``replace_range`` mode from the v2 plan is
  intentionally NOT shipped -- agent-supplied character indices are
  too easy to get wrong.

Already shipped (cumulative):

* PR #1: SQLite mapping store + alias validator.
* PR #2: OAuth helper, ``GoogleDocsClient``, ``story_doc_create``,
  ``hermes story setup`` CLI.
* PR #3: ``story_doc_read``, ``story_doc_append``, ``story_doc_status``.

Out of scope:

* ``story_doc_outline`` -- long-story navigation. Will land when Momo
  has stories long enough to need section-level navigation; current
  largest story is ~2k words.

Activation
----------
Standalone plugins are opt-in via ``plugins.enabled`` in the active
profile's ``config.yaml``. After enabling::

    hermes -p veniceagent plugins enable story_doc

verify with::

    hermes -p veniceagent plugins list | grep story_doc
    hermes -p veniceagent story list             # store still works
    hermes -p veniceagent story setup --check    # auth status

The plugin requires the existing ``[google]`` extra (already declared in
``pyproject.toml`` for the google-workspace skill). If imports fail at
runtime, run::

    pip install 'hermes-agent[google]'

Design notes (anchored to the v2 plan)
--------------------------------------
* No ``check_fn`` on the registered tool. ``tools.registry.get_definitions``
  filters tools whose ``check_fn`` returns False *out of the model's
  schema view*. We need Momo to respond to ``!story start ...`` even
  before OAuth is configured -- so we register unconditionally and let
  the handler return a structured ``tool_error`` JSON the model can
  read aloud.
* ``SKILL.md`` is documentation only. Plugin skills do NOT enter the
  agent's ``<available_skills>`` index automatically (verified against
  ``hermes_cli/plugins.py:454-497``). The dispatch hint
  (``!story start <alias> <prompt>`` -> call this tool) lives in the
  tool's schema description, which the model sees every turn.
* No edits to gateway / model_tools / Momo profile config.
"""

from __future__ import annotations

import logging

from plugins.story_doc.cli import register_cli, story_command
from plugins.story_doc.tools import (
    STORY_DOC_APPEND_SCHEMA,
    STORY_DOC_CREATE_SCHEMA,
    STORY_DOC_READ_SCHEMA,
    STORY_DOC_REVISE_SCHEMA,
    STORY_DOC_STATUS_SCHEMA,
    handle_story_doc_append,
    handle_story_doc_create,
    handle_story_doc_read,
    handle_story_doc_revise,
    handle_story_doc_status,
)

logger = logging.getLogger(__name__)


# (name, schema, handler, emoji) -- declaration table so adding a new
# tool is a one-line change here. Emoji is purely for `hermes tools`
# display per the spotify/google_meet convention.
_TOOLS = (
    ("story_doc_create", STORY_DOC_CREATE_SCHEMA, handle_story_doc_create, "📖"),
    ("story_doc_read",   STORY_DOC_READ_SCHEMA,   handle_story_doc_read,   "👁️"),
    ("story_doc_append", STORY_DOC_APPEND_SCHEMA, handle_story_doc_append, "✍️"),
    ("story_doc_revise", STORY_DOC_REVISE_SCHEMA, handle_story_doc_revise, "✂️"),
    ("story_doc_status", STORY_DOC_STATUS_SCHEMA, handle_story_doc_status, "📊"),
)


def register(ctx) -> None:
    """Plugin entry point.

    PR #4 wires:

    * The ``hermes story`` CLI subcommand tree (PR #1 ``list`` + PR #2
      ``setup``; ``show`` / ``unlink`` are still pending a future PR).
    * Five agent-facing tools into the ``story_doc`` toolset:
      ``story_doc_create``, ``_read``, ``_append``, ``_revise``,
      ``_status``.

    None of the tools carries a ``check_fn`` -- auth checks live in the
    handlers so each tool stays visible to the model even when Google
    OAuth isn't configured (the model surfaces the structured
    ``tool_error`` to the user).
    """
    ctx.register_cli_command(
        name="story",
        help="Story-writer plugin (list stories, set up Google auth)",
        setup_fn=register_cli,
        handler_fn=story_command,
        description=(
            "Manage Hermes story aliases and sync stories to Google Docs. "
            "PR #2 ships `hermes story list` and `hermes story setup` "
            "(--check / --client-secret / --auth-url / --auth-code / "
            "--revoke). PR #3 adds the read/continue/status tools and "
            "PR #4 adds revise (replace_body / replace_match) -- all used "
            "by `!story start/continue/update/revise/status` on Discord."
        ),
    )
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="story_doc",
            schema=schema,
            handler=handler,
            emoji=emoji,
            # Deliberately NO check_fn -- see module docstring + v2 plan
            # section 5.
        )
    logger.debug(
        "story_doc plugin loaded (PR #4: %d tools registered)",
        len(_TOOLS),
    )
