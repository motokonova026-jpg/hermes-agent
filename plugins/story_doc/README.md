# story_doc plugin

Discord-driven story workflow with Google Docs sync. Built per the v2 MVP
plan (`hermes_story_writer_mvp_plan_v2.html` at the repo root). Shipped
in vertical slices; PR #2 lands the first agent tool (`story_doc_create`)
and the OAuth bootstrap.

## What's in PR #5

PR #5 adds deterministic chapter headings for Momo's story continuations:

- `story_doc_create` now ensures a new story starts with `Chapter 1` if the model omits it.
- `story_doc_append` now reads the current Google Doc, finds existing `Chapter N` headings, and ensures the appended continuation starts with the next heading (`Chapter 2`, `Chapter 3`, ...).
- If Momo already generated a leading `Chapter N` heading, the handler preserves it and does not duplicate it.
- Tool results include `chapter_number` and `chapter_heading_added` metadata so Momo can report what happened.

## What's in PR #4

| Path | Purpose |
|---|---|
| `plugin.yaml` | `kind: standalone`, `provides_tools: [story_doc_create, _read, _append, _revise, _status]`. |
| `aliases.py` (PR #1) | Pure `validate_story_key()` enforcing `^[a-z0-9][a-z0-9_-]{0,63}$` and rejecting platform-id-shaped strings. |
| `store.py` (PR #1) | `StoryDocStore` SQLite at `get_hermes_home() / "story_doc" / "mappings.sqlite3"` -- profile-scoped, WAL mode. |
| `oauth.py` (PR #2) | OAuth bootstrap mirroring `skills/productivity/google-workspace/scripts/setup.py`. Token at `get_hermes_home() / "story_doc" / "google_token.json"`, profile-scoped. Scopes: `documents` + `drive.file` only. |
| `client.py` (extended) | `GoogleDocsClient` over `googleapiclient` Docs v1 + Drive v3. PR #3 added `read_doc_text(doc_id)`. PR #4 adds `replace_body(doc_id, new_text)` (atomic delete+insert in one batchUpdate) and `replace_match(doc_id, match_text, replacement_text)` (refuses unless `match_text` occurs exactly once). |
| `tools.py` (extended) | Five agent tools: `story_doc_create` (PR #2), `_read` / `_append` / `_status` (PR #3), `_revise` (PR #4). Still no `check_fn` on any of them; auth checks live in the handlers. |
| `cli.py` (PR #2) | `hermes story list` (PR #1) + `hermes story setup`. |
| `__init__.py` (extended) | `_TOOLS` table now lists all five tools. |

## Tool catalogue (PR #4 surface)

| Tool | Args | Behavior | Discord trigger |
|---|---|---|---|
| `story_doc_create` | `story_key`, `title`, `content` | Create new Doc, insert content, persist alias mapping. Errors `story_key_exists` if the alias is taken. | `!story start <alias> <prompt>` |
| `story_doc_read` | `story_key`, `mode` (`full`/`tail`), `tail_chars?=4000` | Fetch current doc text. Returns `text`, `char_count`, `word_count`, `truncated`, `mode`. | the read step before every `!story continue` / `!story revise` |
| `story_doc_append` | `story_key`, `content`, `separator?="\\n\\n"` | Append `(separator + content)` to end of doc. Bumps `word_count` and refreshes `last_revision_id` in the store. | `!story continue <alias> <instruction>` |
| `story_doc_revise` | `story_key`, `operation_intent` (`targeted_revise`/`broad_update`), `mode` (`replace_body`/`replace_match`), `new_content` (for `replace_body`), `match_text` + `replacement_text` (for `replace_match`). The handler enforces strict (intent, mode) coupling -- only `(targeted_revise, replace_match)` and `(broad_update, replace_body)` are legal. | Atomic Docs `batchUpdate`. `replace_body`: deleteContentRange + insertText. `replace_match`: refuses unless `match_text` is unique in the doc body, then `replaceAllText`. Both modes recompute `word_count` from the post-revise body and refresh `last_revision_id`. | `!story update <alias>` -> `(broad_update, replace_body)`. `!story revise <alias>` -> `(targeted_revise, replace_match)`. |
| `story_doc_status` | `story_key` | Return cached metadata + freshly-fetched Drive `revision_id`, plus `revision_drift` flag if the cached and fresh ids differ. | `!story status <alias>` (informational) |

### Error contract

All five tools return JSON with `{"error": "<machine_key>", "hint": "<actionable_message>", ...extra}`. New machine keys in PR #4:

| `error` value | When | Recovery hint includes |
|---|---|---|
| `revise_match_not_found` | `story_doc_revise` mode `replace_match`: `match_text` not in doc body. | "Use a different snippet from `story_doc_read` output, add more surrounding context, or use mode=replace_body for a broader rewrite". |
| `revise_match_ambiguous` | `match_text` appears 2+ times. Includes `occurrences` count. | "Add more surrounding context to make the snippet unique, or use mode=replace_body for a broader rewrite". |
| `invalid_operation_intent` | Required field missing, or value not in `{targeted_revise, broad_update}`. Echoes back what was received. | Names both legal values and their Discord triggers. |
| `intent_mode_mismatch` | The `(operation_intent, mode)` pair isn't `(targeted_revise, replace_match)` or `(broad_update, replace_body)`. The classic offender: model tries to bypass `revise_match_ambiguous` by switching to `replace_body` under `targeted_revise`. | "Surface the ambiguity error to the user; do NOT switch to replace_body. If they want a full rewrite, they should re-issue as `!story update`." |
| `invalid_new_content` | `replace_body` missing `new_content` or wrong type. | The expected type. |
| `invalid_match_text` | `replace_match` missing/empty `match_text`. | The expected type / non-empty constraint. |
| `invalid_replacement_text` | `replace_match` missing `replacement_text` or wrong type. | The expected type. |

PR #3 keys (`story_key_not_found`, `invalid_mode`, `invalid_tail_chars`, `invalid_separator`) and PR #2 keys (`story_doc_not_*`, `story_key_exists`, `google_docs_error`) are unchanged.

### Safety: `replace_match` and the unique-match guard

Per the v2 plan and Rob's PR #2 directive, `replaceAllText` is **never** called without first verifying the anchor is unique. Sequence:

1. `client.read_doc_text(doc_id)` fetches the current body.
2. `body.count(match_text)` is computed in Python.
3. If count is 0 → raise → handler returns `revise_match_not_found`.
4. If count is ≥2 → raise → handler returns `revise_match_ambiguous` with `occurrences=N`.
5. Only on count == 1 do we call `replaceAllText` with `containsText.text=match_text, matchCase=true` (literal substring match, same semantics as our Python count).

There is a TOCTOU window between the read and the replace — if someone edits the doc in Drive between those calls, a previously-unique anchor could become non-unique. Acceptable for v1: Drive's revision history makes this recoverable (every successful `batchUpdate` produces a revision the user can roll back).

`replace_range` mode (agent-supplied character indices) is intentionally not shipped — too easy for the model to compute the wrong indices from `documents.get`. Use `replace_match` for surgical edits.

### Safety: `operation_intent` and the route-around guard

After Motoko's PR #4 smoke test surfaced that the model could bypass `revise_match_ambiguous` by switching from `mode=replace_match` to `mode=replace_body` within the same tool call, `story_doc_revise` requires a declared `operation_intent` and the handler enforces strict (intent, mode) coupling:

| User Discord trigger | `operation_intent` | `mode` | Effect |
|---|---|---|---|
| `!story revise <alias> <instruction>` | `targeted_revise` | `replace_match` | Surgical edit. Refuses if the anchor isn't unique. |
| `!story update <alias> <instruction>` | `broad_update` | `replace_body` | Full rewrite. Replaces entire body. |

Any other (intent, mode) pair is rejected with `intent_mode_mismatch` *before* any auth check or Docs API call — the handler refuses to ever write a `replace_body` under a `targeted_revise` intent (the exact bypass Motoko caught), and refuses to ever write a `replace_match` under a `broad_update` intent.

The model can still lie about the intent (declare `broad_update` when the user actually typed `!story revise`), but doing so requires actively misrepresenting the user's trigger rather than quietly picking a more permissive mode. The schema description explicitly maps triggers to intents and forbids the route-around — combined with the structural guard, this is the strongest enforcement we can land without editing core gateway code.

If the model surfaces `revise_match_ambiguous` to the user, the user can re-issue as `!story update` for an honest broad rewrite — that's the documented recovery path, not a silent fallback.

## Deployment notes for destructive changes

> **TL;DR:** When you ship a tool change that *locks down* a previously-allowed path, the gateway restart picks up the new code, but old conversation sessions can carry the previous turn's strategy. Clear the active session mapping on every connected platform after the restart, or the model will keep trying the now-forbidden path until the session naturally ends.

This isn't theoretical — Motoko hit it during the PR #4 bypass-fix smoke test. The structural guard worked in code (`intent_mode_mismatch` rejected the bypass), but the resumed Discord DM still carried the model's prior reasoning that `replace_body` was a valid fallback for `!story revise`. Until the DM session was cleared and a fresh one created, the model kept proposing the same forbidden path. After session reset, the very next `!story revise <alias> Replace 'the' with 'a'` produced exactly the intended behaviour: refusal + the recovery hint.

### What counts as a "destructive change"

A change that **forbids a tool call shape that was previously permitted** is destructive. Concretely for `story_doc`:

- Adding a new required parameter that callers had to start providing (e.g. `operation_intent` in PR #4-fix).
- Adding an enforcement path that rejects a previously-legal combination (e.g. `intent_mode_mismatch` when a model picks `replace_body` under `targeted_revise`).
- Renaming or removing a tool, mode, or enum value the model had been calling.
- Tightening a schema (narrower enum, lower length cap, stricter validator) that previously accepted broader input.

A non-destructive change *only adds new permitted shapes* — new tools, new modes, new optional parameters with safe defaults, looser validators. Those don't trigger this protocol.

### Why a gateway restart alone isn't enough

`hermes plugins disable && enable` and a gateway restart will reload `plugin.yaml`, the new tool schemas, and the new handler code. The next *fresh* session will see the new contract. But:

- Discord (and other platforms with persistent thread/DM mappings) resume the prior session by default — same `session_id`, same accumulated message history.
- The model's tool-call reasoning from prior turns is in that history. If it learned a workaround in the old code (e.g. "when ambiguous, switch to `replace_body`"), restarting doesn't unlearn that — the messages are still in the context window.
- Even with the new structural guard catching the unsafe call, the model will *keep proposing* it from the resumed session, blocked each turn, until the session ends naturally.

### Required deployment protocol for destructive changes

1. **Land the change** (PR + tests passing). Run the test suite under `scripts/run_tests.sh` and confirm green.
2. **Restart the gateway.** Picks up new code and schema. Required, but not sufficient.
3. **Clear the active session mapping on every connected platform** for any conversation that *previously exercised the tool you just changed*. For Momo on Discord, that's the per-DM session entry — whatever Motoko's session-clear procedure is for `veniceagent`. (Document the exact command alongside this protocol once it's standardised; for now, see the conversation around session `20260503_103047_83cc4dde` in PR #4 hand-off notes.)
4. **Verify with a fresh session.** Issue a command that *should* be blocked under the new contract. Confirm the model behaves correctly. Then issue a command that *should* still work. Confirm it does.

### Long-term defenses worth considering (not yet built)

- **Tool-version stamping in the schema.** Each tool description carries a `tool_version: "0.4.1"` line; bumping the version invalidates the model's cached strategy for that tool's shape. The framework would need to surface "this tool's contract changed" as part of the system prompt at session start.
- **Deterministic command routing in the gateway.** `gateway/platforms/discord.py` parses `!story revise` server-side and forces the dispatcher path (always `targeted_revise + replace_match`), removing the model's freedom to mis-route. This was deliberately deferred per the v2 plan — it requires editing core gateway code — but it would close the "model can lie about intent" gap that the structural guard alone cannot.

Both of these are bigger pieces of work than the operational checklist above; flag and discuss before opening either as a PR.

## Dependencies

`story_doc` requires the existing `[google]` extra (already declared in
`pyproject.toml` for the `google-workspace` skill — story_doc rides on
that, no new entries added):

```
pip install 'hermes-agent[google]'
```

If `google-api-python-client` or `google-auth-oauthlib` is missing at
runtime, `story_doc_create` will raise on first invocation with a
diagnostic mentioning the install command.

## Activation

Standalone plugins are opt-in per profile.

```
hermes -p veniceagent plugins enable story_doc
hermes -p veniceagent plugins list | grep story_doc
hermes -p veniceagent story setup --check     # auth status
```

To deactivate: `hermes -p veniceagent plugins disable story_doc`. The
SQLite file under `~/.hermes/profiles/veniceagent/story_doc/mappings.sqlite3`
and any token at `~/.hermes/profiles/veniceagent/story_doc/google_token.json`
are left in place when the plugin is disabled.

## OAuth bootstrap (one-time)

Mirrors the muscle memory of the `google-workspace` skill setup so you
don't learn two flows. Headless-friendly: no browser is launched on the
machine where Hermes runs.

### One-time setup

1. **Get a client_secret.json from Google.**
   - Go to <https://console.cloud.google.com/apis/credentials>.
   - Create an OAuth 2.0 Client ID of type **Desktop app**.
   - Enable the **Google Docs API** and **Google Drive API** on the project.
   - Download the JSON.

2. **Store the client secret.** If you already set up the `google-workspace`
   skill, it's at `~/.hermes/profiles/veniceagent/google_client_secret.json`
   and story_doc will reuse it. If not:

   ```
   hermes -p veniceagent story setup --client-secret /path/to/downloaded.json
   ```

   To use a different client (e.g. you want story_doc on a separate GCP
   project): set `STORY_DOC_GOOGLE_CLIENT_SECRET=/path/to/it` in your
   shell.

3. **Get the consent URL.**

   ```
   hermes -p veniceagent story setup --auth-url
   ```

   Open the printed URL in any browser. Complete consent. You'll be
   redirected to a `can't connect` page on `http://localhost:1/` — that's
   expected. **Copy the full URL bar** (it has `?code=...&state=...`).

4. **Exchange the code for a token.**

   ```
   hermes -p veniceagent story setup --auth-code "http://localhost:1/?code=...&state=..."
   ```

   The `--auth-code` arg accepts either the full redirect URL or just
   the bare code value.

5. **Verify.**

   ```
   hermes -p veniceagent story setup --check
   ```

   Should print `AUTHENTICATED: ...`.

### Recovery

If anything goes sideways:

```
hermes -p veniceagent story setup --revoke      # nuke local token
hermes -p veniceagent story setup --auth-url    # start over
```

## What's coming next

- **`story_doc_outline`** — long-story navigation. Will land when
  Momo has stories long enough to need section-level navigation
  (current largest is ~2k words; outline becomes useful around ~8-10k).
- **`hermes story show <alias>` / `unlink <alias>` CLI subcommands** —
  pending; currently use `hermes story list` for inspection.

## Design references

- v2 plan: `hermes_story_writer_mvp_plan_v2.html` (repo root).
- OAuth: `skills/productivity/google-workspace/scripts/setup.py` is the
  canonical Hermes pattern; `oauth.py` is a port with narrowed scopes
  and a profile-scoped token path.
- Tool layout: `plugins/spotify/` is the closest mirror.
- Tests: every module under `plugins/story_doc/` has a paired file under
  `tests/plugins/test_story_doc_*.py`.

## Running the tests

```
scripts/run_tests.sh tests/plugins/test_story_doc_aliases.py \
                     tests/plugins/test_story_doc_store.py \
                     tests/plugins/test_story_doc_oauth.py \
                     tests/plugins/test_story_doc_client.py \
                     tests/plugins/test_story_doc_tools.py \
                     tests/plugins/test_story_doc_continuing_story.py \
                     tests/plugins/test_story_doc_plugin.py
```
