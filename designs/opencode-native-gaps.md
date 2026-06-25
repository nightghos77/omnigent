# OpenCode-native: feature-gap closure plan

**Status:** implemented (single PR) · **Owner:** Dhruv Gupta · **Harness:** `opencode-native`

## Implementation status (this PR)

All gaps from the review are closed in one PR:

- ✅ **Compaction (P0)** — real `/compact` (v1 `/summarize`) + auto-compaction surfacing
- ✅ **MCP** — `spec.mcp_servers` → opencode.json + `permission:ask` (policies route through the engine)
- ✅ **Cost tracking (P1)** — `external_session_usage` from per-message cost/tokens
- ✅ **Resume** — text-prefix replay from the Omnigent transcript (no more silent cross-host amnesia)
- ✅ **Fork (P1)** — text-preamble fork (reuses resume rehydration)
- ✅ **In-harness session-cmd sync** — TUI model-switch mirror + (compact/fork/resume above)
- ✅ **Elicitation** — tool-approval round-trip verified + tested (the review's "double-check")
- ✅ **Policies** — confirmed wired to the TOOL_CALL engine; `permission:ask` closes the MCP coverage hole

Each was live-verified against `opencode serve` 1.17.7 where the wire was uncertain.

**Deferred (bonus, not in the original gap list):** opencode's separate
`question.v2` interactive input-request surface (free-form multi-choice
questions, distinct from tool-approval). Surfacing it to the web requires a
live `question.v2` fixture + the server form-elicitation hook (mirroring
`codex_native_elicitation`), and must coexist with the TUI (first-answer-wins,
like the permission card) — a naive intercept would break TUI interactivity.
Tracked as a follow-up; the tool-approval elicitation path is unaffected.

## Background

`opencode-native` (native-server harness: runner spawns `opencode serve`, an
SSE forwarder translates events, a typed HTTP client injects prompts) merged in
PR #576. A post-merge review of the harness feature matrix flagged gaps. This
doc records a **live recon** of opencode 1.17.7's actual API/event surface, then
gives a per-area gap analysis + plan grounded in that evidence. Reference
sibling throughout is **codex-native** (same native-server shape); the
authoritative capability list is the `harness-integration-guide` skill's
native-harness matrix.

Gap-matrix verdicts for the opencode row (✓ = works, ✗ = missing, ? = unknown):

| Capability | Matrix | Resolved verdict |
|---|---|---|
| Connects to Omnigent MCP | ✗ | missing — build |
| Model override | ✓ | works (per-prompt) |
| Streaming (forwarder) | complete-only | by design for native-server |
| Elicitation (web) | ✓ | **solid** (verified) + a separate `question.v2` surface is unhandled |
| Policies | ? | **YES, wired** to the TOOL_CALL engine (reactive) |
| Cost tracking (P1) | ? | **missing** — build |
| Interrupt | ✓ | works (abort) |
| Bidirectional sync (TUI→Omni) | ✓ | works |
| In-harness session-cmd sync | ✗ | missing — build |
| Resume/fork from Omnigent transcript | ✗ | missing — resume loses history cross-host; fork unwired (P1) |
| Compaction | ? | **missing**, and worse: web-UI `/compact` fakes success (P0) |
| Reasoning (P1) | ✓ | works |
| Images | ✓ | works |

## Recon: opencode 1.17.7 (live)

**Method:** ran `opencode serve` locally (the pinned 1.17.7 is installed on the
dev box), pulled its OpenAPI from `GET /doc` (390 KB), and drove one live
big-pickle turn capturing the `GET /event` SSE stream. Raw artifacts:
`scratchpad/oc-recon/{openapi-1.17.7.json, events.ndjson, RECON-FINDINGS.md}`.
This dispatched the "needs a live server to confirm" blocker on every item.

Key surfaces discovered (all confirmed present in 1.17.7):

- **Compaction events:** auto-compaction emits `session.next.compaction.started` `{sessionID, messageID, reason: auto|manual}` + `…ended` `{…, text, recent}`; an explicit compaction emits `session.compacted` `{sessionID}` (completion only). **Trigger:** the v2 `POST /api/session/{id}/compact` returns **503 "Session compact is not available yet" in 1.17.x** (verified live) — so use the v1 `POST /session/{id}/summarize`, which **requires `{providerID, modelID}`** (read from the session's `model`) and emits `session.compacted`.
- **Cost/context (live-confirmed shape):** `message.updated` assistant `info` carries `cost` (USD) + `tokens:{input,output,reasoning,cache:{read,write}}`; `Session` carries cumulative `cost`+`tokens`; context window = `Model.limit.context`. Event `session.next.context.updated`.
- **MCP:** `opencode.json` `mcp` block — `McpLocalConfig {type:"local", command:[…], cwd?, environment?, enabled?, timeout?}` / `McpRemoteConfig {type:"remote", url, headers?, oauth?, enabled?}`. Runtime API also: `GET/POST /mcp`, `/mcp/{name}/connect`, `/mcp/{name}/auth`.
- **Permission config:** `opencode.json` `permission` — either a scalar `"ask"|"allow"|"deny"` (applies to all tools) or a per-tool map. We synthesize `opencode.json`, so we control it.
- **Resume/history:** `POST /sync/history`, `/sync/replay`, `/sync/start`; `POST /session/{id}/message`; `GET /session/{id}/message`; `POST /session/{id}/fork` (branch at `messageID`).
- **Session commands:** `POST /session/{id}/command`, `GET /command`, event `command.executed`; `/session/{id}/revert` + `/unrevert` (= undo/redo).
- **Questions (elicitation gap):** a surface *separate* from permissions — `question.v2.asked {questions[], tool}` + `/session/{id}/question/{rid}/reply|reject`. The forwarder ignores it today. "Always" decisions persist server-side via `/api/permission/saved`.

## Two clarifications (raised in review)

**1. "The compact button" = the `/compact` slash command.** There is no separate
button. `/compact` is a built-in slash command in both the web composer
(`ap-web` `BUILTIN_SLASH_COMMANDS["/compact"]`) and the REPL
(`omnigent/repl/_repl.py` `@_cmd("/compact")`). The web sends it as
`postEvent({type:"compact"})` (`ap-web/src/store/chatStore.ts:1253`) →
server `_COMPACT_TYPE` (`sessions.py`) → runner control dispatch
(`runner/app.py` ~11523). The runner dispatch only branches on
claude-native/codex-native; **opencode falls to a 204 no-op, so the server then
runs its own AP-side compaction on the Omnigent conversation store** — which is
NOT what opencode sends to the model. Net: `/compact` on an opencode session
emits a `response.compaction.completed` marker while opencode's real context is
untouched (a correctness lie). opencode has a real `POST .../compact`, so we can
make `/compact` genuinely compact opencode. **Recommendation: make it real.**

**2. "Will policies just WORK either way?" — yes, with native config + force-ask.**
- *Precedent:* codex/claude-native expose Omnigent tools via a **relay** — one
  `omnigent` MCP server (`serve-mcp`) that proxies the active toolset; every
  call (incl. MCP) hits the central proxy + policy engine. Guaranteed, but it
  means porting the whole `bridge.json`/`tool_relay.json` relay to opencode (L).
- *Native config path:* we synthesize `opencode.json`, so we write **both** the
  `mcp` block **and** `permission: "ask"`. opencode then emits `permission.asked`
  for tool calls (incl. MCP tools), which the forwarder already routes through
  Omnigent's `TOOL_CALL` policy engine (`opencode_native_permissions.py` +
  `runner/app.py` `_build_opencode_policy_evaluator`) — the same path that
  already gates opencode's built-in tools (confirmed wired + tested). So
  **policies work under native config**, provided we force opencode to ask.
  *Caveat:* a tool opencode is configured to auto-allow would bypass the gate —
  but we own that config, so we don't auto-allow.
- **Recommendation: native `opencode.json` MCP + `permission: ask`.** Far smaller
  than the relay, and policies still "just work." Revisit the relay only if a
  future requirement needs central TOOL_RESULT gating or proxy-side redaction
  (opencode's reactive model can't pre-gate tools opencode never asks about).

## Per-area plan

Each area: **current state → gap → recon evidence → approach → effort/risk.**
All land in `opencode_native_forwarder.py` / `opencode_native_provider.py` /
`runner/app.py` unless noted; server-side contracts are reused as-is.

### 1. Compaction — **P0**
- **Current:** nothing. Auto-compaction is invisible to Omnigent; explicit `/compact` fakes success (see clarification 1).
- **Approach (two parts):**
  - *Surface auto-compaction (additive, no server change):* handle `session.next.compaction.started` → post `external_compaction_status` `in_progress`; `…ended` → `completed`. Reuses claude-native's existing inbound wire contract (`response.compaction.*`). Also drives the web "compacting" marker.
  - *Make `/compact` real:* add `_handle_opencode_native_compact` to the runner control dispatch (mirror `_handle_codex_native_compact`, but HTTP not tmux) that resolves the session's model and calls `POST /session/{id}/summarize` via the client, returning 200 so the server stops running the AP-side fake (204 when no live server → graceful fallback; 503 on failure). Completion flows back through the `session.compacted` / `…ended` handler.
- **Effort:** S–M · **Risk:** low for surfacing; medium for the dispatch (touches the shared runner control path + the server's compact-fallback semantics — scope carefully so codex/claude are unaffected).

### 2. MCP
- **Current:** none; agent MCP tools absent in opencode.
- **Approach:** in `opencode_native_provider.py`, add `build_opencode_mcp_block(spec.mcp_servers)`: stdio → `{type:"local", command:[cmd,*args], environment:env}`; http → `{type:"remote", url, headers}` (+ resolve `databricks_profile` → `Authorization: Bearer` header, reusing `resolve_databricks_gateway`'s pattern). Merge into the synthesized `opencode.json` alongside `provider`/`model` in the `runner/app.py` spawn flow. Set `permission: "ask"` so MCP tool calls route through the policy engine (clarification 2). Secrets ride the existing atomic-0600 writer.
- **Effort:** S–M · **Risk:** low (gated on `spec.mcp_servers`; reuses the 0600 writer + spawn chokepoint).

### 3. Resume — **high**
- **Current:** resumes only by the persisted opencode `external_session_id`. Same-host relaunch works (per-session `XDG_DATA_HOME` persists opencode's store). **Cross-host / wiped-store resume silently starts an empty session — the web transcript shows history but the agent has amnesia, no error.**
- **Approach:** when `get_session(external_session_id)` returns `None` on a resume that *had* an id, (C) at minimum surface the failure instead of silent amnesia, then (A) rehydrate from the Omnigent transcript: `GET /v1/sessions/{id}/items` (mirror codex's paginated fetch) → seed a fresh opencode session via `POST /session/{id}/message` and/or the `/sync/history`/`/sync/replay` primitives. Confirm the `/sync/history` body shape against the live server before committing to it.
- **Effort:** M · **Risk:** medium — hinges on how opencode accepts back-dated/non-executing history (token cost, tool-call representation). Ship (C) first.

### 4. Cost tracking — **P1**
- **Current:** none; `message.updated` cost/tokens dropped. Context ring, cost badge, and cost-budget policy all dead for opencode.
- **Approach:** in the forwarder, accumulate `info.cost` + `info.tokens` per assistant `message.updated`; post `external_session_usage {context_tokens, context_window, cumulative_cost_usd, cumulative_*_tokens, model}` (context_window from `Model.limit.context`) on message.updated + `session.idle`. Reuses codex's `external_session_usage` contract verbatim; server prices via `cumulative_cost_usd` directly. Live-confirmed token/cost shape.
- **Effort:** M · **Risk:** low (additive; cosmetic worst case).

### 5. Fork — **P1**
- **Current:** `transport.fork()` + `POST /session/{id}/fork` exist but are wired to nothing; opencode is absent from `_FORK_HISTORY_NATIVE_HARNESSES`.
- **Approach:** add `opencode-native` to `_FORK_HISTORY_NATIVE_HARNESSES` (`sessions.py`); add `fork_source_*` fields to the opencode launch config + a fork branch in `_auto_create_opencode_terminal` that calls `client.fork(source, {messageID})` for same-harness sources, falling back to the resume-rehydration path (#3) for cross-family sources. Simpler than codex (opencode has a first-class fork endpoint). Build on #3.
- **Effort:** M · **Risk:** low–medium.

### 6. In-harness session-cmd sync
- **Current:** neither direction. Omnigent `/compact` (and clear/fork/resume) don't reach opencode; TUI-typed `/model`, `/compact`, `/undo` don't mirror back.
- **Approach:** Omnigent→opencode via `POST /session/{id}/command` (the matrix's "clear/fork/resume/switch"); the `/compact` half is covered by #1. opencode→Omnigent: handle `command.executed` (+ mirror `/model` to `model_override`, surface `/compact`/`/undo` as `slash_command` items). Overlaps #1/#3/#5; do last.
- **Effort:** M–L · **Risk:** low–medium.

### 7. Elicitation (verify) + Policies (verify/harden)
- **Elicitation:** ✓ solid (full permission.v2 round-trip, fail-closed, tested). Harden: (C1) the typed `transport.reply_permission` is dead code parallel to the live forwarder path — unify or delete to prevent drift; (C2) a failed `POST .../reply` is swallowed → opencode-side hang — retry/reconcile via `GET /session/{id}/permission`. **New (C3):** handle the separate `question.v2.asked` input-request surface (currently ignored) as a form elicitation. Effort S (C1) / M (C2, C3).
- **Policies:** ✓ wired (allow/deny/ask all honored). Reactive only — coverage bounded by opencode's permission surface; no pre-tool hook, no TOOL_RESULT phase. Force-ask via the synthesized config (clarification 2) closes the coverage hole; document the reactive model. Effort S.

## Recommended sequence

1. **P0 compaction** (surface auto-compaction + make `/compact` real)
2. **MCP** (native config + `permission: ask`)
3. **Resume** (surface failure → rehydrate from transcript)
4. **Cost tracking** (P1)
5. **Fork** (P1; builds on resume)
6. **Session-cmd sync** (builds on 1/3/5)
7. **Elicitation/policy hardening** (C1–C3 + force-ask)

Each is an independent, reviewable PR. 1–5 reuse existing server contracts (no
server changes except the compact-dispatch arm in #1).

## Open questions

1. `/sync/history` request-body shape — verify against the live server before choosing it for resume rehydration (vs. re-injecting via `POST /session/{id}/message`).
2. opencode's behavior for back-dated/non-executing history messages (cost, ordering, tool-call representation) — gates resume Option A.
3. Whether to ever build the MCP relay (central TOOL_RESULT gating) — deferred; native config + force-ask is the plan.
4. `question.v2` payload (`QuestionV2Info`/`QuestionV2Tool`) — capture a real fixture to shape the form-elicitation mapping (C3).
