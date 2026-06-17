# OpenCode Native Harness and Unified Harness Interface Design

**Status:** Draft design for review. Read-only investigation completed on 2026-06-17. No Omnigent source or tests were modified.

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Evidence Baseline](#evidence-baseline)
   1. [Current Harness Contract](#current-harness-contract)
   2. [ExecutorAdapter and Thin Harness Wrappers](#executoradapter-and-thin-harness-wrappers)
   3. [Native Server Pattern: Codex Native](#native-server-pattern-codex-native)
   4. [Current Scattered Registries](#current-scattered-registries)
   5. [Current Terminal Resource Flow](#current-terminal-resource-flow)
3. [Investigation Findings](#investigation-findings)
   1. [I1: OpenCode TUI Attach and Server Surface](#i1-opencode-tui-attach-and-server-surface)
   2. [I2: Polly/Debby Sub-Agent Harness Selection](#i2-pollydebby-sub-agent-harness-selection)
   3. [I3: Terminal/TUI Reuse in Omnigent](#i3-terminaltui-reuse-in-omnigent)
4. [A. OpenCode Harness Core](#a-opencode-harness-core)
5. [B. OpenCode TUI Support](#b-opencode-tui-support)
6. [C. Web UI Integration](#c-web-ui-integration)
7. [D. OpenCode as Optional Runtime-Selectable Harness for Polly and Debby](#d-opencode-as-optional-runtime-selectable-harness-for-polly-and-debby)
8. [E. Unified Add-a-Harness Interface](#e-unified-add-a-harness-interface)
9. [Sequencing and PR Breakdown](#sequencing-and-pr-breakdown)
10. [Risk Register](#risk-register)
11. [Full Test Matrix](#full-test-matrix)
12. [Appendix A: Extension-Point Checklist](#appendix-a-extension-point-checklist)
13. [Appendix B: Landing-PR References and Review Notes](#appendix-b-landing-pr-references-and-review-notes)

---

## Executive Summary

OpenCode should land as a new canonical Omnigent harness named `opencode-native`. Architecturally, it matches the `codex-native` family more than the in-process Python SDK family: Omnigent should own a per-conversation native server process, inject web turns into that server, forward the server's stream into Omnigent's session stream, and optionally launch a separate terminal TUI attached to the same native server/session.

The critical uncertainty for TUI support is resolved: OpenCode **does** support attaching its TUI to an externally running server. The command is `opencode attach <url>`, with `--dir`, `--session`, `--continue`, `--fork`, `--username`, and `--password` flags; `--password` defaults to `OPENCODE_SERVER_PASSWORD`, and `--username` defaults to `OPENCODE_SERVER_USERNAME` or `opencode` (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:7`, `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:35`, `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:40`). This gives Omnigent the same product shape as Codex Native: the runner owns the server and forwarder, while a human can open/take over the TUI in a terminal.

The main design constraints are:

- OpenCode's programmatic surface is HTTP + SSE, not Codex's WebSocket JSON-RPC.
- OpenCode's source is in an active v1/v2 transition; the cloned source reports `opencode` version `1.17.7`, so Omnigent must pin and version-check the CLI/API (`/tmp/opencode/packages/opencode/package.json:3`).
- Polly and Debby cannot select a sub-agent harness through existing `sys_session_send` arguments. Today `sys_session_send` accepts `args.input`, `args.purpose`, and `args.model`, but not `args.harness` (`omnigent/tools/builtins/spawn.py:242`, `omnigent/tools/builtins/spawn.py:265`, `omnigent/tools/builtins/spawn.py:279`). Short-term runtime selection should be through declared optional `opencode` sub-agent types; long-term runtime selection should add an allowlisted `args.harness` override.
- Harness registration is scattered across runtime module maps, spec allowlists, aliases, model support, onboarding/readiness, install metadata, built-in agents, and web registries. OpenCode can land first using the current pattern, but the follow-up should introduce a `HarnessDescriptor` registry and `NativeServerHarness` transport abstraction.

Recommended sequence: land `opencode-native` first in focused PRs, prove core + TUI + web + orchestrator behavior, then extract the common native-server interface and derive the scattered registries from `HarnessDescriptor`.

---

## Evidence Baseline

### Current Harness Contract

A harness in Omnigent is a per-conversation HTTP service. The runtime harness package describes each harness module as exporting a zero-argument `create_app() -> FastAPI`, and the runner imports that module and serves the returned app over a Unix socket (`omnigent/runtime/harnesses/__init__.py:12`, `omnigent/runtime/harnesses/__init__.py:27`). The harness scaffold documents the required API subset: `POST /v1/sessions/{conversation_id}/events`, `GET /health`, heartbeat emission, cancellation, steering, and graceful shutdown (`omnigent/runtime/harnesses/_scaffold.py:5`).

The real execution contract is the inner `Executor` interface. `Executor.run_turn(messages, tools, system_prompt, config)` is the abstract async generator that harness backends implement (`omnigent/inner/executor.py:495`, `omnigent/inner/executor.py:501`). Capability methods include `supports_streaming`, `supports_tool_calling`, `handles_tools_internally`, `close_session`, `interrupt_session`, and `enqueue_session_message` (`omnigent/inner/executor.py:518`, `omnigent/inner/executor.py:524`, `omnigent/inner/executor.py:537`, `omnigent/inner/executor.py:546`, `omnigent/inner/executor.py:550`).

For native coding harnesses like Codex/OpenCode, `handles_tools_internally()` must return true: OpenCode executes shell/file/tool actions inside its own loop and Omnigent should forward those actions as observed events rather than re-execute them. The base `Executor` docstring for `handles_tools_internally` explicitly says that when true, the session should not re-execute tool calls and should pass through tool request/completion events as informational (`omnigent/inner/executor.py:524`).

### ExecutorAdapter and Thin Harness Wrappers

`ExecutorAdapter` is the bridge from the inner `Executor` interface to the harness HTTP contract. It subclasses `HarnessApp`, accepts an `executor_factory`, lazily constructs the executor on first turn, caches it for the conversation lifetime, and translates executor events into harness stream events (`omnigent/runtime/harnesses/_executor_adapter.py:137`, `omnigent/runtime/harnesses/_executor_adapter.py:142`, `omnigent/runtime/harnesses/_executor_adapter.py:149`, `omnigent/runtime/harnesses/_executor_adapter.py:216`).

Thin harness wrappers already follow the pattern. For example, the Codex Native harness module imports `ExecutorAdapter`, constructs `CodexNativeExecutor`, and returns `adapter.build()` from `create_app()` (`omnigent/inner/codex_native_harness.py:1`). OpenCode should add the same kind of thin wrapper:

```python
# omnigent/inner/opencode_native_harness.py
from fastapi import FastAPI
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.inner.opencode_native_executor import OpenCodeNativeExecutor


def _build_opencode_native_executor() -> OpenCodeNativeExecutor:
    return OpenCodeNativeExecutor()


def create_app() -> FastAPI:
    adapter = ExecutorAdapter(executor_factory=_build_opencode_native_executor)
    return adapter.build()
```

### Native Server Pattern: Codex Native

Codex Native is the closest existing architecture.

Codex's native server manager builds an argv of `codex app-server --listen <url>` and starts it as a subprocess (`omnigent/codex_native_app_server.py:560`, `omnigent/codex_native_app_server.py:561`, `omnigent/codex_native_app_server.py:563`, `omnigent/codex_native_app_server.py:570`). Codex's process is launched with a private `CODEX_HOME`, ensuring native state and hooks are session-scoped (`omnigent/codex_native_app_server.py:569`).

The Codex Native executor injects Omnigent web turns into the app-server. If a turn is active, it uses `turn/steer`; otherwise it uses `turn/start` with the current thread id and input items (`omnigent/inner/codex_native_executor.py:216`, `omnigent/inner/codex_native_executor.py:217`, `omnigent/inner/codex_native_executor.py:219`). After successful injection, it yields a `TurnComplete` because streaming is handled by the forwarder rather than by the executor directly (`omnigent/inner/codex_native_executor.py:231`).

Codex's TUI is a separate process attached to the already-running app-server. `build_codex_remote_args()` explicitly documents that the TUI attaches over `--remote` so the terminal, forwarder, and web-UI message bridge all drive the same thread (`omnigent/codex_native_app_server.py:1511`, `omnigent/codex_native_app_server.py:1521`, `omnigent/codex_native_app_server.py:1522`). This is the model OpenCode should mirror, substituting `opencode serve` and `opencode attach` for Codex's app-server/remote transport.

### Current Scattered Registries

Harness registration is currently split across many files:

| Concern | Current location | Evidence |
|---|---|---|
| Runtime module loading | `_HARNESS_MODULES` | `omnigent/runtime/harnesses/__init__.py:34` |
| Spec validation allowlist | `OMNIGENT_HARNESSES` | `omnigent/spec/_omnigent_compat.py:76` |
| User-facing aliases | `HARNESS_ALIASES` | `omnigent/harness_aliases.py:9` |
| Native harness recognition | `NATIVE_HARNESSES` | `omnigent/harness_aliases.py:26` |
| Model override support | `harness_supports_model_override()` | `omnigent/model_override.py:211` |
| CLI install metadata | `_HARNESS_INSTALL` | `omnigent/onboarding/harness_install.py:99` |
| CLI-backed harness map | `_HARNESS_NAME_TO_KEY` | `omnigent/onboarding/harness_install.py:140` |
| Built-in agent discovery | `GET /v1/agents` loading `loaded.spec.executor.harness_kind` | `omnigent/server/routes/builtin_agents.py:89` |
| Web native agent list | `NATIVE_CODING_AGENTS` | `ap-web/src/lib/nativeCodingAgents.ts:21` |
| Web brain harness labels | `BRAIN_HARNESS_LABELS` | `ap-web/src/lib/agentLabels.ts:8` |
| Optional Python package extras | `[project.optional-dependencies]` | `pyproject.toml:92` |

The current runtime map includes `claude-native`, `codex-native`, `pi-native`, `cursor`, and `antigravity`, but no OpenCode (`omnigent/runtime/harnesses/__init__.py:34`, `omnigent/runtime/harnesses/__init__.py:41`, `omnigent/runtime/harnesses/__init__.py:43`, `omnigent/runtime/harnesses/__init__.py:51`, `omnigent/runtime/harnesses/__init__.py:64`, `omnigent/runtime/harnesses/__init__.py:71`). The spec allowlist similarly includes current harnesses but not OpenCode (`omnigent/spec/_omnigent_compat.py:76`).

### Current Terminal Resource Flow

Omnigent already has the terminal substrate OpenCode needs.

- `terminal_attach_url()` builds terminal attach WebSocket paths under `/v1/sessions/{session}/resources/terminals/{terminal}/attach` (`omnigent/native_terminal.py:27`, `omnigent/native_terminal.py:42`).
- `bind_session_runner()` binds a native terminal session to the runner that will host it (`omnigent/native_terminal.py:49`).
- Terminal resources are projected as `SessionResourceView(type="terminal")` by `terminal_resource_view()` (`omnigent/entities/session_resources.py:136`, `omnigent/entities/session_resources.py:150`).
- Terminal metadata includes `terminal_name`, `session_key`, running state, tmux socket, and tmux target (`omnigent/entities/session_resources.py:156`).
- The server resource stream snapshots child sessions and terminals on connect (`omnigent/server/routes/sessions.py:16892`, `omnigent/server/routes/sessions.py:16944`).

The Web UI recognizes terminal-first native agents through `nativeWrapperLabelsForAgent()`, which sets `omnigent.ui=terminal` and `omnigent.wrapper=<wrapper>` labels (`ap-web/src/lib/nativeCodingAgents.ts:106`, `ap-web/src/lib/nativeCodingAgents.ts:112`).

---

## Investigation Findings

### I1: OpenCode TUI Attach and Server Surface

#### OpenCode version and packaging caveat

The cloned OpenCode source at `/tmp/opencode` reports package version `1.17.7` for the shipping `opencode` package (`/tmp/opencode/packages/opencode/package.json:3`) and has a `bin` entry named `opencode` (`/tmp/opencode/packages/opencode/package.json:18`). The newer package `@opencode-ai/cli` also reports version `1.17.7`, but its binary is `lildax`, showing that the repo has multiple package surfaces in flight (`/tmp/opencode/packages/cli/package.json:3`, `/tmp/opencode/packages/cli/package.json:4`, `/tmp/opencode/packages/cli/package.json:7`).

Design consequence: Omnigent must pin a known OpenCode package/version and version-check the runtime CLI. User context says the npm package is `opencode-ai`; the cloned monorepo's shipping package is named `opencode`. The implementation PR must verify the currently published package name and lock it in install/readiness metadata. This design assumes the executable is `opencode`, because the package bin entry confirms that in the source (`/tmp/opencode/packages/opencode/package.json:18`).

#### `opencode serve`

OpenCode has a headless server command:

- `ServeCommand` is defined in `/tmp/opencode/packages/opencode/src/cli/cmd/serve.ts:6`.
- The command is named `serve` (`/tmp/opencode/packages/opencode/src/cli/cmd/serve.ts:7`).
- It is described as starting a headless OpenCode server (`/tmp/opencode/packages/opencode/src/cli/cmd/serve.ts:9`).
- It resolves shared network options (`/tmp/opencode/packages/opencode/src/cli/cmd/serve.ts:18`).
- It calls `Server.listen(opts)` (`/tmp/opencode/packages/opencode/src/cli/cmd/serve.ts:19`).
- It logs `opencode server listening on http://...` (`/tmp/opencode/packages/opencode/src/cli/cmd/serve.ts:20`).

Network options are shared by TUI and serve:

- `port` exists and defaults to `0` in source (`/tmp/opencode/packages/opencode/src/cli/network.ts:6`, `/tmp/opencode/packages/opencode/src/cli/network.ts:7`, `/tmp/opencode/packages/opencode/src/cli/network.ts:10`).
- `hostname` exists and defaults to `127.0.0.1` (`/tmp/opencode/packages/opencode/src/cli/network.ts:12`, `/tmp/opencode/packages/opencode/src/cli/network.ts:15`).
- mDNS and CORS options also exist (`/tmp/opencode/packages/opencode/src/cli/network.ts:17`, `/tmp/opencode/packages/opencode/src/cli/network.ts:27`).
- `resolveNetworkOptionsNoConfig()` may override CLI defaults from config unless the flags were explicitly present (`/tmp/opencode/packages/opencode/src/cli/network.ts:46`, `/tmp/opencode/packages/opencode/src/cli/network.ts:51`, `/tmp/opencode/packages/opencode/src/cli/network.ts:53`).

Design consequence: Omnigent should always choose and pass an explicit loopback port and host:

```bash
opencode serve --hostname 127.0.0.1 --port <allocated_port>
```

Do not rely on default port `4096` or source default `0`; the docs and examples mention `4096`, but the code's option default is `0` and config can override it.

#### `opencode attach`

OpenCode has an explicit TUI attach command:

- `AttachCommand` is defined in `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:7`.
- The command is `attach <url>` (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:8`).
- It is described as attaching to a running OpenCode server (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:9`).
- The positional `url` example is `http://localhost:4096` (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:12`, `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:14`).
- `--dir` selects the directory to run in (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:17`).
- `--continue`, `--session`, and `--fork` are supported (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:21`, `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:26`, `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:31`).
- `--password` defaults to `OPENCODE_SERVER_PASSWORD` (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:35`).
- `--username` defaults to `OPENCODE_SERVER_USERNAME` or `opencode` (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:40`).
- The handler builds auth headers from username/password (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:63`).
- The handler validates the session against the target URL (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:67`).
- The handler runs the TUI with `url`, `sessionID`, `directory`, and `headers` (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:82`, `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:84`, `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:89`, `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:92`).

Exact Omnigent TUI takeover command:

```bash
OPENCODE_SERVER_PASSWORD=<secret> \
  opencode attach http://127.0.0.1:<port> \
  --dir <workspace> \
  --session <opencode_session_id>
```

If argv secret exposure is acceptable in a local trusted runner, the equivalent is:

```bash
opencode attach http://127.0.0.1:<port> \
  --dir <workspace> \
  --session <opencode_session_id> \
  --username opencode \
  --password <secret>
```

Preferred design: put the password in the environment so it does not appear in process argv, because the attach command's password option explicitly defaults from `OPENCODE_SERVER_PASSWORD` (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:35`).

#### TUI default command is not enough

The default TUI command can start an external server when network flags are present, but that path is not attach-to-existing-server:

- The TUI command calculates `external` when `--port`, `--hostname`, `--mdns`, nonzero port, or non-loopback hostname is used (`/tmp/opencode/packages/opencode/src/cli/cmd/tui.ts:148`, `/tmp/opencode/packages/opencode/src/cli/cmd/tui.ts:149`, `/tmp/opencode/packages/opencode/src/cli/cmd/tui.ts:155`).
- In external mode, it calls the worker's `server` RPC and uses the returned URL (`/tmp/opencode/packages/opencode/src/cli/cmd/tui.ts:157`, `/tmp/opencode/packages/opencode/src/cli/cmd/tui.ts:159`).
- In internal mode, it uses an in-memory `http://opencode.internal` transport with worker fetch and event source (`/tmp/opencode/packages/opencode/src/cli/cmd/tui.ts:163`).

For Omnigent, the correct attach path is `opencode attach <url>`, not `opencode --port ...`.

#### REST/SSE API surface

The generated OpenAPI at `/tmp/opencode/packages/sdk/openapi.json` exposes the key routes Omnigent needs:

| Route | Operation | Use |
|---|---|---|
| `GET /event` | `event.subscribe` | SSE stream |
| `POST /session` | `session.create` | Create OpenCode session |
| `GET /session/{sessionID}/message` | `session.messages` | Load history |
| `POST /session/{sessionID}/message` | `session.prompt` | Send message |
| `POST /session/{sessionID}/prompt_async` | `session.prompt_async` | Send async message |
| `POST /session/{sessionID}/abort` | `session.abort` | Cancel active work |
| `POST /session/{sessionID}/fork` | `session.fork` | Fork session |
| `GET /permission` | `permission.list` | List pending permissions |
| `POST /permission/{requestID}/reply` | `permission.reply` | Reply to permission request |

The source route group also defines these paths under `SessionPaths`: `list`, `status`, `get`, `messages`, `message`, `create`, `fork`, `abort`, `prompt`, `promptAsync`, and session-scoped `permissions` (`/tmp/opencode/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:80`, `/tmp/opencode/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:95`, `/tmp/opencode/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:96`, `/tmp/opencode/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:101`).

The event bridge forwards core events onto the global bus with a payload of `{ id, type, properties }`, optionally scoped by directory/workspace (`/tmp/opencode/packages/opencode/src/event-v2-bridge.ts:38`, `/tmp/opencode/packages/opencode/src/event-v2-bridge.ts:42`, `/tmp/opencode/packages/opencode/src/event-v2-bridge.ts:46`). The server connected event is defined as `server.connected` (`/tmp/opencode/packages/opencode/src/server/event.ts:4`, `/tmp/opencode/packages/opencode/src/server/event.ts:5`).

### I2: Polly/Debby Sub-Agent Harness Selection

#### Polly today

Polly is a multi-agent coding orchestrator with fixed declared sub-agents:

- Polly's main spec is `examples/polly/config.yaml`.
- The prompt says Polly has exactly three sub-agents (`examples/polly/config.yaml:59`).
- `claude_code` is a Claude Code worker using `claude-native` (`examples/polly/config.yaml:63`).
- `codex` is a Codex worker using `codex-native` (`examples/polly/config.yaml:64`).
- `pi` is a Pi worker using `pi` (`examples/polly/config.yaml:65`).
- The Claude worker spec sets `executor.type: omnigent` and `harness: claude-native` (`examples/polly/agents/claude_code/config.yaml:5`, `examples/polly/agents/claude_code/config.yaml:8`).
- The Codex worker spec sets `executor.type: omnigent` and `harness: codex-native` (`examples/polly/agents/codex/config.yaml:5`, `examples/polly/agents/codex/config.yaml:8`).

Polly's top-level brain is separate from its workers:

- The brain is `executor.type: omnigent` (`examples/polly/config.yaml:27`).
- The brain harness is `claude-sdk` (`examples/polly/config.yaml:31`).
- README documents that `omnigent run examples/polly/ --harness pi` changes the orchestrator brain and that sub-agents keep their own harnesses (`README.md:175`, `README.md:176`).

#### Debby today

Debby is a two-headed brainstorming partner with fixed declared sub-agents:

- Debby's main spec is `examples/debby/config.yaml`.
- It says every question is fanned out to both Claude and GPT responders (`examples/debby/config.yaml:3`).
- It says the two heads run on `claude-sdk` and `openai-agents` (`examples/debby/config.yaml:15`).
- The brain uses `executor.type: omnigent` and `harness: claude-sdk` (`examples/debby/config.yaml:35`, `examples/debby/config.yaml:38`).
- The prompt says Debby has exactly two plain sub-agents (`examples/debby/config.yaml:42`).
- The two sub-agents are `claude` (`claude-sdk`) and `gpt` (`openai-agents`) (`examples/debby/config.yaml:44`, `examples/debby/config.yaml:45`).
- Debby is bundled and registered by the server; `_DEBBY_AGENT_NAME = "debby"` and `_DEBBY_BUNDLE_SOURCE` points at the packaged `examples/debby` bundle (`omnigent/server/app.py:79`, `omnigent/server/app.py:86`).

#### `sys_session_send` today has no `args.harness`

Sub-agent tooling exposes named sub-agent dispatch, not arbitrary harness override:

- `SysSessionSendTool` has two addressing modes: named `(agent, title)` and by `session_id` (`omnigent/tools/builtins/spawn.py:64`).
- The `agent` parameter's enum is dynamically derived from declared sub-agent names (`omnigent/tools/builtins/spawn.py:157`, `omnigent/tools/builtins/spawn.py:176`).
- The object form of `args` allows only `input`, `purpose`, and `model` (`omnigent/tools/builtins/spawn.py:242`, `omnigent/tools/builtins/spawn.py:248`, `omnigent/tools/builtins/spawn.py:254`, `omnigent/tools/builtins/spawn.py:265`).
- The schema sets `additionalProperties: False`, so `args.harness` is rejected today (`omnigent/tools/builtins/spawn.py:279`).
- `args.model` is documented as create-time-only and uses the selected child harness's existing model plumbing (`omnigent/tools/builtins/spawn.py:265`, `omnigent/tools/builtins/spawn.py:272`).

The runner dispatch path confirms the child's harness is the declared sub-agent harness:

- `_subagent_harness()` resolves the declared harness from the parent spec's named sub-agent (`omnigent/runner/tool_dispatch.py:800`, `omnigent/runner/tool_dispatch.py:817`).
- Child session creation sends `agent_id`, `parent_session_id`, `title`, and `sub_agent_name`; no `harness_override` is included (`omnigent/runner/tool_dispatch.py:1075`, `omnigent/runner/tool_dispatch.py:1079`).
- When `model` is passed, dispatch validates it against the resolved child harness and persists only `model_override` (`omnigent/runner/tool_dispatch.py:1081`, `omnigent/runner/tool_dispatch.py:1084`, `omnigent/runner/tool_dispatch.py:1091`, `omnigent/runner/tool_dispatch.py:1101`).

Top-level `harness_override` exists, but it is not a sub-agent override:

- `Conversation.harness_override` is a per-session override for the bound agent's brain (`omnigent/entities/conversation.py:115`).
- It is set at session creation and immutable because the runner spawns the harness on the first turn (`omnigent/entities/conversation.py:119`, `omnigent/entities/conversation.py:120`).
- The comment explicitly states that sub-agent sessions never inherit it and that Polly workers keep their declared harnesses when the brain is overridden (`omnigent/entities/conversation.py:124`).

Design consequence: OpenCode can be optional/runtime-selectable for Polly and Debby immediately only by adding declared sub-agent types, e.g. `agent: "opencode"`. A future `args.harness` feature must be explicit and allowlisted.

### I3: Terminal/TUI Reuse in Omnigent

OpenCode can reuse the existing terminal machinery:

- Native terminal attach URLs are generic and not Codex-specific (`omnigent/native_terminal.py:27`).
- Terminal resources are generic session resources (`omnigent/entities/session_resources.py:136`).
- The runner already has native terminal auto-create branches for `claude-native`, `codex-native`, and `pi-native` (`omnigent/runner/app.py:10518`, `omnigent/runner/app.py:10570`, `omnigent/runner/app.py:10604`).
- Codex's auto-create function imports Codex app-server, remote args, bridge prep, and resume helpers (`omnigent/runner/app.py:884`, `omnigent/runner/app.py:893`). OpenCode should add a parallel `_auto_create_opencode_terminal()` branch.
- The Web UI has a generic native registry and wrapper labels (`ap-web/src/lib/nativeCodingAgents.ts:21`, `ap-web/src/lib/nativeCodingAgents.ts:106`).

---

## A. OpenCode Harness Core

### A.1 Goals

Add `opencode-native` as a first-class Omnigent harness with the following properties:

- Runs a per-conversation `opencode serve` process owned by the runner.
- Uses an HTTP + SSE Python client generated or typed from `/tmp/opencode/packages/sdk/openapi.json` for the pinned OpenCode version.
- Persists bridge state including OpenCode session id, server URL, data/config dirs, model, active turn/message state, and permission state.
- Injects Omnigent web turns through OpenCode REST endpoints.
- Streams OpenCode SSE events into Omnigent session events and external transcript items.
- Supports cancellation through `POST /session/{sessionID}/abort`.
- Supports resume through persisted OpenCode session ids and controlled OpenCode data dirs.
- Supports model pinning where the OpenCode API/config supports it.
- Maps OpenCode permissions into Omnigent policy/approval cards.
- Supports optional TUI attach without stopping the forwarder.

### A.2 Canonical Harness ID and User-Facing Names

Canonical id:

```text
opencode-native
```

Suggested built-in wrapper agent:

```text
opencode-native-ui
```

Suggested wrapper label:

```text
opencode-native-ui
```

Optional aliases after canonical support is stable:

```text
opencode
native-opencode
```

Do not add aliases in the first PR unless there is a user-facing need. The canonical id should be used in specs, registry entries, telemetry, and tests.

### A.3 New and Changed Files

#### New Omnigent backend files

| File | Purpose |
|---|---|
| `omnigent/inner/opencode_native_harness.py` | Thin `create_app()` wrapper around `ExecutorAdapter`. |
| `omnigent/inner/opencode_native_executor.py` | Inner `Executor` implementation for `opencode-native`. |
| `omnigent/opencode_native_app_server.py` | Process manager for `opencode serve`, attach argv builder, env/config setup. |
| `omnigent/opencode_native_client.py` | Typed HTTP/SSE client generated or shaped from OpenCode OpenAPI. |
| `omnigent/opencode_native_bridge.py` | Bridge directory, state files, secret handling, XDG dirs. |
| `omnigent/opencode_native_forwarder.py` | SSE consumer and OpenCode-to-Omnigent event mapper. |
| `omnigent/opencode_native_state.py` | Durable launch/session state helpers. |
| `omnigent/opencode_native.py` | Optional CLI wrapper for `omnigent opencode`. |
| `omnigent/opencode_native_permissions.py` | Optional permission normalization helpers if not shared. |

#### Changed Omnigent backend files

| File | Change |
|---|---|
| `omnigent/runtime/harnesses/__init__.py` | Add `"opencode-native": "omnigent.inner.opencode_native_harness"`. |
| `omnigent/spec/_omnigent_compat.py` | Add `opencode-native` to `OMNIGENT_HARNESSES`. |
| `omnigent/harness_aliases.py` | Add native id/aliases if aliases are accepted. |
| `omnigent/model_override.py` | Mark `opencode-native` as model-override-capable if model plumbing is implemented. |
| `omnigent/model_catalog.py` | Add OpenCode model/provider resolution or passthrough mapping. |
| `omnigent/onboarding/harness_install.py` | Add OpenCode CLI metadata and `opencode-native` -> install key mapping. |
| `omnigent/onboarding/harness_readiness.py` | Include OpenCode in configured harness readiness. |
| `omnigent/server/app.py` | Seed `opencode-native-ui` built-in agent if product requires a top-level card. |
| `omnigent/_wrapper_labels.py` | Add wrapper label constant. |
| `omnigent/runner/app.py` | Add auto-create terminal branch and native event handling. |
| `pyproject.toml` | Add optional extra only if Python deps are needed; CLI is external npm, so likely no Python extra. |

#### New tests

| Test file | Scope |
|---|---|
| `tests/unit/test_opencode_native_client.py` | HTTP/SSE client behavior. |
| `tests/unit/test_opencode_native_forwarder.py` | SSE translation and dedupe. |
| `tests/unit/test_opencode_native_executor.py` | Run-turn injection, abort, queue. |
| `tests/unit/test_opencode_native_app_server.py` | Process argv/env/health/version behavior. |
| `tests/integration/test_opencode_native_spawn.py` | Harness process spawn with fake OpenCode server. |
| `tests/e2e/test_opencode_native_terminal.py` | Gated real CLI/TUI test. |

### A.4 Server Process Manager

Implement `OpenCodeNativeServer` in `omnigent/opencode_native_app_server.py`.

Responsibilities:

1. Resolve `opencode` executable.
2. Validate version against a pinned/tested range.
3. Allocate a loopback port.
4. Create a per-session bridge root.
5. Create per-session XDG data/config roots.
6. Generate auth secret for the OpenCode server if the server supports basic auth via `OPENCODE_SERVER_PASSWORD`.
7. Write per-session OpenCode config if model/provider/system behavior requires it.
8. Launch:

```bash
opencode serve --hostname 127.0.0.1 --port <port>
```

9. Poll readiness through the OpenCode HTTP API.
10. Expose `base_url`, `auth_headers`, `bridge_dir`, `data_home`, `config_home`, and process handle.
11. Terminate the process on session close or runner shutdown.

Proposed launch env:

```python
env = {
    **filtered_parent_env,
    "XDG_DATA_HOME": str(bridge_dir / "xdg-data"),
    "XDG_CONFIG_HOME": str(bridge_dir / "xdg-config"),
    "OPENCODE_SERVER_PASSWORD": secret,
    "OMNIGENT_OPENCODE_NATIVE_BRIDGE_DIR": str(bridge_dir),
}
```

Provider env injection should mirror Codex/Pi native provider handling. Codex Native routes across configured provider/login modes before app-server launch (`omnigent/runner/app.py:910`, `omnigent/runner/app.py:916`). OpenCode should similarly prefer Omnigent setup credentials over global OpenCode login state.

Security posture:

- Bind only to `127.0.0.1`.
- Use a random password even on loopback if OpenCode supports it.
- Do not expose the OpenCode server URL directly to remote clients; Web UI attaches to Omnigent terminal resources, not to OpenCode HTTP.
- Treat OpenCode's server as runner-internal.

### A.5 HTTP + SSE Python Client

Implement `OpenCodeClient` in `omnigent/opencode_native_client.py`.

The client should be generated from, or at least validated against, `/tmp/opencode/packages/sdk/openapi.json` for pinned version `1.17.7`. Vendoring the full generated client may be heavy; a pragmatic first implementation can define a thin typed wrapper over the required endpoints plus schema fixtures from the OpenAPI.

Minimum API:

```python
@dataclass(frozen=True)
class OpenCodeSession:
    id: str
    title: str | None = None
    parent_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class OpenCodeEvent:
    id: str | None
    type: str
    properties: dict[str, Any]
    raw: dict[str, Any]

class OpenCodeClient:
    def __init__(self, base_url: str, *, headers: Mapping[str, str] | None = None): ...

    async def create_session(self, payload: Mapping[str, Any] | None = None) -> OpenCodeSession: ...
    async def get_session(self, session_id: str) -> OpenCodeSession: ...
    async def list_messages(self, session_id: str) -> list[dict[str, Any]]: ...
    async def get_message(self, session_id: str, message_id: str) -> dict[str, Any]: ...
    async def prompt(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]: ...
    async def prompt_async(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]: ...
    async def abort(self, session_id: str) -> bool: ...
    async def fork(self, session_id: str, payload: Mapping[str, Any] | None = None) -> OpenCodeSession: ...
    async def list_permissions(self) -> list[dict[str, Any]]: ...
    async def reply_permission(self, request_id: str, reply: Mapping[str, Any]) -> bool: ...
    async def events(self, *, directory: str | None = None, workspace: str | None = None) -> AsyncIterator[OpenCodeEvent]: ...
```

Client details:

- Use `httpx.AsyncClient` for REST and streaming.
- Parse SSE according to standard `event:`/`data:` framing.
- OpenCode's event bridge publishes payloads with `id`, `type`, and `properties` (`/tmp/opencode/packages/opencode/src/event-v2-bridge.ts:42`, `/tmp/opencode/packages/opencode/src/event-v2-bridge.ts:46`).
- Keep unknown fields in `raw` for forward-compatible logging and fixture updates.
- Add `directory` or `x-opencode-directory` handling after confirming OpenCode's workspace routing headers/query parameters in the pinned API. The server route comments say `serve` loads instances per request via `x-opencode-directory` (`/tmp/opencode/packages/opencode/src/cli/cmd/serve.ts:10`).

### A.6 Bridge State

Implement durable bridge state in `omnigent/opencode_native_bridge.py` and `omnigent/opencode_native_state.py`.

Suggested state file:

```json
{
  "version": 1,
  "omnigent_session_id": "conv_abc123",
  "opencode_session_id": "ses_abc123",
  "workspace": "/repo/path",
  "server_base_url": "http://127.0.0.1:49231",
  "bridge_dir": "/home/user/.omnigent/opencode-native/<hash>",
  "xdg_data_home": "/home/user/.omnigent/opencode-native/<hash>/xdg-data",
  "xdg_config_home": "/home/user/.omnigent/opencode-native/<hash>/xdg-config",
  "active_message_id": "msg_...",
  "active_part_ids": ["prt_..."],
  "status": "idle",
  "model_override": "openai/gpt-5.4",
  "last_event_id": "evt_...",
  "pending_permissions": {},
  "created_at": "...",
  "updated_at": "..."
}
```

Bridge helpers:

```python
def bridge_root() -> Path: ...
def prepare_bridge_dir(session_id: str) -> Path: ...
def state_path(bridge_dir: Path) -> Path: ...
def read_bridge_state(bridge_dir: Path) -> OpenCodeBridgeState | None: ...
def write_bridge_state(bridge_dir: Path, state: OpenCodeBridgeState) -> None: ...
def xdg_data_home_for_bridge_dir(bridge_dir: Path) -> Path: ...
def xdg_config_home_for_bridge_dir(bridge_dir: Path) -> Path: ...
def auth_secret_path(bridge_dir: Path) -> Path: ...
```

Persistence policy:

- Persist OpenCode session id into Omnigent `Conversation.external_session_id` once known. The entity field is explicitly designed for runtime-native session ids such as Claude/Codex ids (`omnigent/entities/conversation.py:135`).
- Preserve OpenCode XDG state across runner restarts for local resume.
- Clear only volatile server URL/process pid on restart.

### A.7 Inner Executor

Implement `OpenCodeNativeExecutor` in `omnigent/inner/opencode_native_executor.py`.

Capabilities:

```python
class OpenCodeNativeExecutor(Executor):
    def supports_streaming(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    async def interrupt_session(self, session_key: str) -> bool:
        ... # POST /session/{id}/abort

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        ... # queue, prompt_async if idle, or abort+prompt by policy
```

Run-turn flow:

1. Compute session key and bridge dir.
2. Ensure `OpenCodeNativeServer` is running.
3. Ensure `OpenCodeClient` is connected.
4. Load or create OpenCode session:
   - if `external_session_id` exists, use it;
   - else `POST /session`, then patch Omnigent session `external_session_id`.
5. Ensure forwarder is running for the OpenCode server/session.
6. Convert Omnigent messages to an OpenCode prompt payload.
7. Add model pin if configured and supported.
8. If OpenCode session idle: send via `POST /session/{id}/prompt_async`.
9. If OpenCode session busy: choose steer/queue policy.
10. Yield `TurnComplete(response=None)` after successful injection, because forwarder owns streaming, matching Codex Native's injection/completion split (`omnigent/inner/codex_native_executor.py:231`).

#### Prompt injection endpoint

The design should support both sync and async endpoints:

- Primary: `POST /session/{sessionID}/prompt_async` for non-blocking native-server parity.
- Fallback: `POST /session/{sessionID}/message` if pinned OpenCode behavior requires synchronous prompt admission.

The user's required phrasing mentions prompt injection via `POST /session/{id}/message`; the design should implement that endpoint and prefer `prompt_async` where it is verified to be the correct non-blocking admission API.

#### Message conversion

OpenCode prompt payload should include:

- User text.
- Attachments/files if supported by OpenCode prompt schema.
- Agent/model hints if supported.
- System prompt only if OpenCode API supports a per-session/per-prompt instruction override; otherwise write a per-session OpenCode agent/config file before session creation.

Open question: exact schema for `SessionPrompt.PromptInput` must be locked during implementation by inspecting `/tmp/opencode/packages/opencode/src/session/prompt.ts` and the generated OpenAPI schema.

### A.8 Steer-or-Queue Semantics

Codex Native supports active-turn steering through `turn/steer` (`omnigent/inner/codex_native_executor.py:216`). OpenCode's exposed v1 REST routes do not show a direct steer endpoint. Therefore OpenCode must use explicit queue semantics.

Proposed behavior:

| OpenCode state | Incoming Omnigent message | Behavior |
|---|---|---|
| idle | normal user message | `POST /session/{id}/prompt_async` or `/message`. |
| busy, no pending queued message | normal user message | Store Omnigent-side pending prompt and admit when `session.status` becomes idle. |
| busy, user requested interrupt | interrupt then prompt | `POST /session/{id}/abort`, wait for idle, then prompt. |
| busy, TUI human active | web message | Either queue with clear UI status or reject with “OpenCode is busy in TUI”. |
| permission pending | approval result | `POST /permission/{requestID}/reply`; do not admit unrelated prompt until permission resolved. |

OpenCode v2 `session.next.*` events suggest an internal queue exists. Tests mention `session.next.prompt.admitted`, `session.next.prompt.promoted`, and `session.next.interrupt.requested` (`/tmp/opencode/packages/core/test/session-prompt.test.ts:195`, `/tmp/opencode/packages/core/test/session-prompt.test.ts:196`, `/tmp/opencode/packages/core/test/session-prompt.test.ts:111`). If a stable v2 queue API is available in the pinned version, Omnigent can replace the local queue with native queue endpoints in a follow-up.

### A.9 Forwarder

Implement `OpenCodeNativeForwarder` in `omnigent/opencode_native_forwarder.py`.

Responsibilities:

- Connect to `GET /event` and keep the SSE stream alive.
- Filter events by OpenCode session id and workspace/directory.
- Load existing messages on startup to seed dedupe state.
- Translate OpenCode events/parts to Omnigent session stream events and external transcript items.
- Track active/idle status.
- Resolve pending permission approvals.
- Persist `external_session_id` and active status.
- Reconnect SSE on transient failures with backoff.
- Log unknown events with bounded payloads.

Dedupe keys:

```text
opencode:<sessionID>:<messageID>
opencode:<sessionID>:<messageID>:<partID>
opencode:<eventID>
```

#### SSE-event to Omnigent-event translation table

| OpenCode event/source | Evidence | Meaning | Omnigent translation | Notes |
|---|---|---|---|---|
| `server.connected` | `/tmp/opencode/packages/opencode/src/server/event.ts:5` | SSE connected to server | Forwarder ready/debug log; no transcript item | Use as readiness signal only. |
| Global bus payload `{id,type,properties}` | `/tmp/opencode/packages/opencode/src/event-v2-bridge.ts:42`, `/tmp/opencode/packages/opencode/src/event-v2-bridge.ts:46` | Generic event envelope | Parse `type` and dispatch by table | Preserve raw event for debugging. |
| `message.part.updated` | `/tmp/opencode/packages/core/src/v1/session.ts:615` | Message part changed | Text delta, tool status update, file diff update, or assistant item update | Inspect part type/state. Dedupe by part id. |
| `message.part.removed` | `/tmp/opencode/packages/core/src/v1/session.ts:624` | Message part removed | Tombstone/remove external item if Omnigent supports it; otherwise append debug status | Avoid deleting user-visible history unless server supports external tombstones. |
| `message.part.delta` | `/tmp/opencode/packages/opencode/src/session/message-v2.ts:62` | Incremental part delta | Stream assistant text chunk or tool progress delta | Prefer for low-latency text. |
| `session.status` or status endpoint update | `/tmp/opencode/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:121` | Session active/idle/completed state | Session child status update, response in-progress/completed | Completion should be emitted on transition to idle after active prompt. |
| `session.next.prompt.admitted` | `/tmp/opencode/packages/core/test/session-prompt.test.ts:195` | Prompt accepted into queue | Omnigent status: queued/admitted | Useful for queued web turns. |
| `session.next.prompt.promoted` | `/tmp/opencode/packages/core/test/session-prompt.test.ts:196` | Queued prompt became active | Omnigent status: in progress | Correlate active message id. |
| `session.next.interrupt.requested` | `/tmp/opencode/packages/core/test/session-prompt.test.ts:111` | Interrupt requested | Omnigent status: cancelling | Pair with abort endpoint. |
| Permission request event, likely `permission.asked` | User-provided established finding; permission route evidence in `/tmp/opencode/packages/opencode/src/server/routes/instance/httpapi/groups/permission.ts` | OpenCode needs approval | Omnigent approval/elicitation card | Exact payload schema must be fixture-tested. |
| Tool part `state.status = pending/running` | Tool part statuses appear in `/tmp/opencode/packages/opencode/src/session/message-v2.ts:360` | Tool call started or running | Observed `ToolCallRequest`/tool progress block | Since tools are internal, do not execute. |
| Tool part `state.status = completed` | Completed part handling appears in `/tmp/opencode/packages/opencode/src/session/message-v2.ts:303` | Tool call completed | Observed `ToolCallComplete`/result block | Cap large outputs. |
| Tool part `state.status = error` | Error part handling appears in `/tmp/opencode/packages/opencode/src/session/message-v2.ts:336` | Tool call failed | Observed failed tool block and maybe `ExecutorError` if fatal | Distinguish recoverable tool error from turn failure. |
| Assistant text part completed | Message part schema in `/tmp/opencode/packages/core/src/v1/session.ts` | Assistant message final | Finalize text item | Dedupe against deltas. |
| User message saved | Session messages endpoint in `/tmp/opencode/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:179` | User prompt persisted | Optional observed user item if from TUI | Avoid duplicating Omnigent-originated user messages. |
| Unknown event | N/A | Forward compatibility | Debug external item/log only | Never crash forwarder on unknown event. |

#### Omnigent transcript policy

- Omnigent-originated user prompts already exist in Omnigent history; do not duplicate them when OpenCode echoes them.
- TUI-originated user prompts should be mirrored into Omnigent as external user items so the web transcript stays complete.
- Tool calls and file changes should be rendered as observed events, not as Omnigent tool dispatches.
- Permission cards should be session-scoped and deduped by OpenCode request id.

### A.10 Permissions to Policy/Approval Mapping

OpenCode exposes a permission list and reply API:

- `GET /permission` lists pending permissions in the OpenAPI.
- `POST /permission/{requestID}/reply` replies to permission requests in the OpenAPI.
- Source permission route group imports `PermissionV1` and defines permission response payloads (`/tmp/opencode/packages/opencode/src/server/routes/instance/httpapi/groups/permission.ts`).
- The attach command supports basic auth to talk to secured servers (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:63`).

Design mapping:

| OpenCode permission reply | Omnigent decision | Behavior |
|---|---|---|
| `once` | approve once | Reply once to OpenCode; do not persist allow. |
| `always` | approve always | Reply always to OpenCode; optionally persist scoped Omnigent remembered decision. |
| `reject` | deny | Reply reject; show denial in transcript. |

Policy flow:

1. Forwarder receives permission request event or polls pending list after event.
2. Normalize permission into Omnigent policy evaluation input:
   - action/tool name;
   - command/path/URL if present;
   - working directory;
   - OpenCode session id;
   - current Omnigent session id;
   - harness `opencode-native`.
3. Run configured Omnigent policy if available.
4. If policy returns allow/deny, reply to OpenCode immediately.
5. If policy needs user input, emit Omnigent approval card.
6. On user approval result, call `reply_permission()`.
7. Deduplicate by OpenCode `requestID`.

Open questions:

- Exact permission request payload fields and reply enum names in `PermissionV1.Reply`.
- Whether OpenCode has separate “question” requests in v2 that should map to `request_user_input` rather than tool approval.
- How to scope `always`: per session, per workspace, per command pattern, or OpenCode native saved permission.

### A.11 Model Pinning

OpenCode TUI supports `--model provider/model` in the normal TUI command (`/tmp/opencode/packages/opencode/src/cli/cmd/tui.ts:80`, `/tmp/opencode/packages/opencode/src/cli/cmd/tui.ts:83`). The attach command does **not** expose `--model`; it passes only `continue`, `sessionID`, and `fork` in args (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:87`).

Design requirements:

- Model pinning must happen at the server/session/prompt/config level, not by relying on `opencode attach --model`.
- `OpenCodeNativeExecutor` should apply `ExecutorConfig.model_override` to prompt payload if OpenCode supports model in `SessionPrompt.PromptInput`.
- If prompt-level model is unavailable, write a per-session OpenCode config under controlled `XDG_CONFIG_HOME` before session creation.
- If neither is stable, support model pin only through OpenCode TUI `/model` mirroring and document limitations.

Omnigent changes:

- Add `opencode-native` to model override support only after model plumbing is verified (`omnigent/model_override.py:211`).
- Add family mismatch logic if OpenCode can be pinned to model families that must be validated.
- Expose OpenCode model options only if OpenCode has a model list endpoint or config catalog.

### A.12 Resume and Fork

Resume data sources:

- Omnigent `Conversation.external_session_id` stores OpenCode session id.
- Bridge state stores XDG dirs and metadata.
- OpenCode persists sessions under XDG data dirs.

Resume flow:

1. Runner starts for Omnigent session.
2. Load `external_session_id` from conversation.
3. Prepare bridge dir and XDG dirs.
4. Start `opencode serve` with same XDG dirs.
5. Validate session exists through `GET /session/{id}`.
6. If found, start forwarder and optionally attach TUI with `--session <id>`.
7. If not found, rebuild a new OpenCode session from Omnigent history or start fresh with a warning.

Fork flow:

- Prefer `POST /session/{source}/fork` because the OpenAPI exposes `session.fork`.
- Store returned OpenCode session id on target Omnigent conversation.
- If source data is unavailable on current host, rebuild from Omnigent transcript.

### A.13 Codex-Native to OpenCode Mapping Table

| Codex Native concept | Evidence | OpenCode Native equivalent | Design notes |
|---|---|---|---|
| Harness id `codex-native` | `omnigent/runtime/harnesses/__init__.py:43` | `opencode-native` | Add to same runtime map. |
| Thin harness wrapper | `omnigent/inner/codex_native_harness.py:1` | `omnigent/inner/opencode_native_harness.py` | Same `ExecutorAdapter` pattern. |
| Executor | `omnigent/inner/codex_native_executor.py:143` | `OpenCodeNativeExecutor` | Inject HTTP prompt, forwarder streams. |
| App-server process | `codex app-server --listen` at `omnigent/codex_native_app_server.py:560` | `opencode serve --hostname 127.0.0.1 --port P` | HTTP server, not WS JSON-RPC. |
| Transport | WebSocket JSON-RPC | HTTP REST + SSE | Implement `OpenCodeHttpTransport`. |
| Turn start | `turn/start` (`omnigent/inner/codex_native_executor.py:216`) | `POST /session/{id}/message` or `/prompt_async` | Prefer async admission. |
| Turn steer | `turn/steer` (`omnigent/inner/codex_native_executor.py:216`) | Omnigent queue or abort+prompt | No v1 direct steer endpoint found. |
| Interrupt | `turn/interrupt` established and runner comments around interrupt | `POST /session/{id}/abort` | OpenAPI has `session.abort`. |
| Resume thread | `thread/resume` + external session id | `GET /session/{id}` + `opencode attach --session` | Persist OpenCode session id. |
| Forwarder input | Codex app-server event stream | `GET /event` SSE | Parse `{id,type,properties}`. |
| TUI attach | `codex ... --remote <url>` (`omnigent/codex_native_app_server.py:1511`) | `opencode attach <url> --dir W --session S` | Attach exists. |
| Private state dir | `CODEX_HOME` (`omnigent/codex_native_app_server.py:569`) | `XDG_DATA_HOME` / `XDG_CONFIG_HOME` | Keep per-session. |
| Policy hooks | Codex hook files (`omnigent/codex_native_app_server.py:545`) | OpenCode permission API | No hook install unless OpenCode supports plugins/hooks. |
| Permission reply | Codex elicitation JSON-RPC | `POST /permission/{requestID}/reply` | Map once/always/reject. |
| Model config | Codex `-c` and `--model`/remote config | OpenCode prompt/config/TUI model | Attach has no `--model`; pin elsewhere. |
| Terminal resource | Runner `_auto_create_codex_terminal` | `_auto_create_opencode_terminal` | Same resource/attach machinery. |
| Built-in wrapper | `codex-native-ui` | `opencode-native-ui` | Add UI card. |

### A.14 Open Questions for Section A

1. Exact prompt payload schema for model, agent, and system prompt.
2. Exact permission request and reply schemas.
3. Whether OpenCode server supports a health endpoint or readiness must use `/event`/`/session/status`.
4. Whether SSE supports last-event-id resume.
5. Whether OpenCode server auth is controlled entirely by `OPENCODE_SERVER_PASSWORD` or also config.
6. Whether OpenCode can run fully with provider env from Omnigent setup without writing global OpenCode config.
7. Whether OpenCode can expose file diffs in a route that should be mirrored into Omnigent changed-files resources.

---

## B. OpenCode TUI Support

### B.1 Goal

Allow a human to open and take over an OpenCode TUI attached to the same OpenCode server/session that Omnigent is driving through web chat. The forwarder must remain live while the TUI is attached so human TUI actions continue to appear in Omnigent's transcript and web UI.

### B.2 Confirmed External Attach Command

OpenCode attach is available and should be the primary TUI path:

```bash
OPENCODE_SERVER_PASSWORD=<secret> \
  opencode attach http://127.0.0.1:<port> \
  --dir <workspace> \
  --session <opencode_session_id>
```

Evidence:

- `attach <url>` command (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:8`).
- Described as attaching to a running server (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:9`).
- Supports `--dir` (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:17`).
- Supports `--session` (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:26`).
- Supports password via env fallback (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:35`).
- Runs TUI with URL/session/directory/headers (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:82`).

### B.3 Runner-Owned Serve + Terminal Topology

```text
Omnigent server
  └─ browser connects to Omnigent session stream and terminal attach WS

Omnigent runner
  ├─ harness FastAPI process: opencode-native
  ├─ native server: opencode serve --hostname 127.0.0.1 --port P
  ├─ forwarder task: GET http://127.0.0.1:P/event
  └─ terminal tmux pane: opencode attach http://127.0.0.1:P --dir W --session S
```

Key rule: the TUI process is disposable. The OpenCode server/session and Omnigent forwarder are the durable control plane.

### B.4 Backend Flow

1. User opens an `opencode-native` session or sub-agent.
2. Runner resolves harness as `opencode-native`.
3. Runner starts or resumes `opencode serve`.
4. Runner starts `OpenCodeNativeForwarder` on `/event`.
5. Runner creates terminal resource for the session.
6. Runner launches tmux command:

```bash
opencode attach <server_url> --dir <workspace> --session <external_session_id>
```

7. Browser attaches to Omnigent terminal WebSocket, not OpenCode directly.
8. Human interacts with OpenCode TUI.
9. OpenCode server emits SSE events.
10. Forwarder mirrors TUI-originated messages/tool actions into Omnigent.

### B.5 New Runner Code

Add to `omnigent/runner/app.py`:

```python
async def _auto_create_opencode_terminal(
    *,
    session_id: str,
    launch_config: NativeLaunchConfig,
    agent_spec: AgentSpec | None,
    server_client: httpx.AsyncClient | None,
) -> SessionResourceView:
    ...
```

Responsibilities:

- Read persisted `terminal_launch_args`, `external_session_id`, `model_override`, and workspace.
- Prepare bridge dir.
- Start or reuse OpenCode server.
- Create/resume OpenCode session.
- Start forwarder.
- Build attach argv.
- Launch terminal through existing terminal registry.
- Return `terminal_resource_view()`.

Add call-site branch parallel to existing native branches:

```python
if _harness == "opencode-native":
    terminal_view = await _auto_create_opencode_terminal(...)
```

Existing branches for native terminals are in `omnigent/runner/app.py:10518`, `omnigent/runner/app.py:10570`, and `omnigent/runner/app.py:10604`.

### B.6 Attach Arg Builder

Implement in `omnigent/opencode_native_app_server.py`:

```python
def build_opencode_attach_args(
    *,
    opencode_args: tuple[str, ...] = (),
    server_url: str,
    workspace: str,
    session_id: str | None,
) -> list[str]:
    args = ["attach", server_url, "--dir", workspace]
    if session_id:
        args.extend(["--session", session_id])
    args.extend(opencode_args)
    return args
```

For auth, prefer env:

```python
def opencode_terminal_env(server: OpenCodeNativeServer) -> dict[str, str]:
    return {
        "OPENCODE_SERVER_PASSWORD": server.auth_secret,
        "OPENCODE_SERVER_USERNAME": "opencode",
        "XDG_DATA_HOME": str(server.xdg_data_home),
        "XDG_CONFIG_HOME": str(server.xdg_config_home),
    }
```

### B.7 Keeping Forwarder Live While Attached

The forwarder should be independent of TUI process lifetime:

- Starting TUI should not cancel/restart the forwarder.
- Closing browser terminal should not stop `opencode serve` if a turn is active.
- TUI process exit should mark terminal resource not running but leave server/forwarder alive if the session remains active.
- Server/forwarder should stop only on session close, explicit native terminal shutdown, or runner shutdown.

Deduplication is critical because both web and TUI can touch the same OpenCode session. The forwarder should tag items by OpenCode message/part ids rather than by origin.

### B.8 `/model` Mirroring

Current Omnigent model updates are persisted as `model_override` and published to live harnesses; server code publishes model-change events when a session is patched (`omnigent/server/routes/sessions.py:13325`). Runner has native-specific model-change handling for Claude/Codex around `omnigent/runner/app.py:10076`.

OpenCode model mirroring design:

1. If OpenCode REST exposes a session model update endpoint, use it.
2. Else if OpenCode prompt payload supports model, apply new model on next prompt and show "takes effect next turn".
3. Else if OpenCode TUI control can execute `/model` or update prompt/session state, use the TUI control route only when a TUI is running.
4. Else write per-session OpenCode config and require server restart for model changes.

Important evidence: default TUI command supports `--model` (`/tmp/opencode/packages/opencode/src/cli/cmd/tui.ts:80`), but `attach` does not pass model in its args (`/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:87`). Therefore model mirroring cannot rely on attach argv.

### B.9 Resume Relaunch

On resume:

1. Read Omnigent `external_session_id`.
2. Start `opencode serve` with the bridge's XDG dirs.
3. Validate `GET /session/{external_session_id}`.
4. Restart forwarder.
5. Recreate terminal resource if terminal-first session UI is opened.
6. Launch:

```bash
opencode attach http://127.0.0.1:<new_port> --dir <workspace> --session <external_session_id>
```

Note that the server URL changes across restarts; the OpenCode session id does not.

### B.10 Fallback If Attach Were Unavailable

Attach **is available** in the investigated version, so this is only a future-proof fallback.

If a future OpenCode version removes `opencode attach`:

- Keep core headless support through `opencode serve` + REST/SSE.
- Provide "Open in OpenCode" as a best-effort terminal launch using the same `XDG_DATA_HOME` and `--session` if the normal TUI supports it.
- Only allow TUI launch while OpenCode is idle to avoid split-brain server ownership.
- Treat TUI as a separate follow-up process, not a live takeover.
- Mark `has_terminal_ui=false` in `HarnessDescriptor` until attach parity returns.

---

## C. Web UI Integration

### C.1 Goals

Expose OpenCode in AP Web as:

1. A top-level native coding agent card (`OpenCode`).
2. A terminal-first native session.
3. A sub-agent session that appears in the Subagents panel and can be opened/taken over.
4. A setup-aware harness requiring the `opencode` CLI.
5. Optionally, a brain harness override for bundle agents after core support is proven.

### C.2 Files and Symbols to Touch

| File | Symbol/area | Change |
|---|---|---|
| `ap-web/src/lib/nativeCodingAgents.ts` | `NativeCodingAgentIconKind` | Add `"opencode"`. |
| `ap-web/src/lib/nativeCodingAgents.ts` | `NATIVE_CODING_AGENTS` | Add OpenCode spec. |
| `ap-web/src/lib/nativeCodingAgents.ts` | `nativeCodingAgentForHarness` | Works automatically after registry addition. |
| `ap-web/src/lib/agentLabels.ts` | `BRAIN_HARNESS_LABELS` | Add only if `opencode-native` is allowed as bundle brain. |
| `ap-web/src/hooks/useAvailableAgents.ts` | `displayNameForAgent` | Should resolve OpenCode through native registry. |
| `ap-web/src/shell/NewChatDialog.tsx` | `BrainHarnessOptions` | Include OpenCode only if in `BRAIN_HARNESS_LABELS`. |
| `ap-web/src/shell/NewChatDialog.tsx` | session create labels | Ensure `nativeWrapperLabelsForAgent()` covers OpenCode. |
| `ap-web/src/shell/SubagentsPanel.tsx` | `iconForAgentType` | Add OpenCode icon/type mapping. |
| `ap-web/src/hooks/useTerminals.ts` | terminal resources | Likely no protocol change; add tests. |
| `ap-web/src/components/AgentCard.tsx` | native display | Ensure OpenCode card renders description/setup. |
| `ap-web/src/components/AgentInfo.tsx` | harness info | Add OpenCode label/details if hard-coded. |
| `ap-web/src/shell/TerminalFirstContext.tsx` | wrapper handling | Ensure OpenCode wrapper label routes terminal-first. |
| `ap-web/src/shell/sidebarNav.ts` | native wrapper mapping | Should work through `nativeCodingAgents`. |

### C.3 Native Registry Addition

Current native registry has Claude, Codex, and Pi (`ap-web/src/lib/nativeCodingAgents.ts:21`). Add:

```ts
export type NativeCodingAgentIconKind = "claude" | "codex" | "pi" | "opencode";

export const NATIVE_CODING_AGENTS = [
  ...,
  {
    key: "opencode",
    agentName: "opencode-native-ui",
    harness: "opencode-native",
    wrapperLabel: "opencode-native-ui",
    displayName: "OpenCode",
    iconKind: "opencode",
    sortRank: 25,
    capabilities: ["approvalMode"],
  },
] as const satisfies readonly NativeCodingAgentSpec[];
```

`nativeWrapperLabelsForAgent()` already derives terminal labels from registry entries (`ap-web/src/lib/nativeCodingAgents.ts:106`). After adding OpenCode, New Chat session creation should automatically include terminal-first labels for an OpenCode native wrapper session.

### C.4 Agent Labels and Brain Harness Picker

`BRAIN_HARNESS_LABELS` is for bundle-agent brain overrides (`ap-web/src/lib/agentLabels.ts:8`). The comment says native terminal wrappers are deliberately absent today (`ap-web/src/lib/agentLabels.ts:10`).

Recommendation:

- Do **not** add `opencode-native` to `BRAIN_HARNESS_LABELS` in the first web PR unless `opencode-native` can robustly act as a non-terminal bundle-agent brain.
- Add it later if OpenCode can honor arbitrary system prompts/tools and model pins as a general `executor.type: omnigent` harness.

If added later:

```ts
export const BRAIN_HARNESS_LABELS: Record<string, string> = {
  ...,
  "opencode-native": "OpenCode",
};
```

The New Chat harness picker renders entries from `BRAIN_HARNESS_LABELS` (`ap-web/src/shell/NewChatDialog.tsx:654`) and sends `harness_override` on session create (`ap-web/src/shell/NewChatDialog.tsx:1200`).

### C.5 NewChatDialog

Current flow:

- Available agents are loaded through `useAvailableAgents()` (`ap-web/src/shell/NewChatDialog.tsx:685`).
- Agents are sorted with native rank first (`ap-web/src/shell/NewChatDialog.tsx:696`, `ap-web/src/shell/NewChatDialog.tsx:700`).
- Native labels are computed before session create (`ap-web/src/shell/NewChatDialog.tsx:1153`).
- `harness_override` is sent for brain overrides (`ap-web/src/shell/NewChatDialog.tsx:1200`).

OpenCode changes:

- Add OpenCode to `NATIVE_CODING_AGENTS`; sorting and labels should work.
- Add setup warning via `harnessUnconfiguredOnHost(id, host)` if host readiness reports OpenCode missing (`ap-web/src/shell/NewChatDialog.tsx:665`).
- If OpenCode is top-level native only, it should be a normal agent card, not a brain harness radio row.

### C.6 useAvailableAgents

Current available-agent shape includes `harness` (`ap-web/src/hooks/useAvailableAgents.ts:15`). Display name resolution already checks native harness/name mappings (`ap-web/src/hooks/useAvailableAgents.ts:34`). Once the server returns `harness: "opencode-native"`, OpenCode display should be automatic.

Backend requirement: built-in agent list must load the OpenCode wrapper spec and return `harness = loaded.spec.executor.harness_kind` (`omnigent/server/routes/builtin_agents.py:89`).

### C.7 SubagentsPanel

For Polly, OpenCode will appear as a child session with `sub_agent_name="opencode"` and harness `opencode-native`. The Subagents panel should:

- Display OpenCode icon/name.
- Show status preview from child session updates.
- Allow opening the child session.
- Prefer terminal-first navigation when the child has an OpenCode terminal resource.

Changes:

- Add OpenCode icon mapping in `iconForAgentType`.
- Avoid any hard-coded assumption that only `claude_code`, `codex`, and `pi` are coding workers.
- Tests should include an `opencode` child session.

### C.8 useTerminals and TerminalFirstContext

No protocol change should be needed. OpenCode terminal resources should look like existing terminal resources:

```json
{
  "type": "terminal",
  "metadata": {
    "terminal_name": "opencode",
    "session_key": "main",
    "running": true,
    "tmux_socket": "...",
    "tmux_target": "..."
  }
}
```

`useTerminals()` should discover them as it does for Codex/Pi. `TerminalFirstContext` should route OpenCode sessions using the wrapper label from `nativeCodingAgents`.

### C.9 AgentCard and AgentInfo

OpenCode should show:

- Display name: `OpenCode`.
- Harness: `opencode-native`.
- Install requirement: `opencode` CLI.
- Description from the built-in agent spec.
- Terminal-first behavior.

If icons are hard-coded, add `opencode` icon kind. If not, use a generic terminal/code icon first.

### C.10 Web Tests

Add/update:

- `ap-web/src/lib/nativeCodingAgents.test.ts` if present, or add coverage in existing tests.
- `ap-web/src/hooks/useAvailableAgents.test.tsx`: OpenCode harness maps to display name.
- `ap-web/src/shell/NewChatDialog.flow.test.tsx`: OpenCode creates terminal-first labels.
- `ap-web/src/shell/SubagentsPanel.test.tsx`: OpenCode child session row and navigation.
- `ap-web/src/components/AgentCard.test.tsx`: OpenCode card display.

---

## D. OpenCode as Optional Runtime-Selectable Harness for Polly and Debby

### D.1 Current Constraint

There is no `args.harness` today. Runtime selection can only choose among declared sub-agent names. Therefore the short-term design must add OpenCode as an optional declared sub-agent, while the long-term design can add an explicit harness override.

### D.2 Short-Term Polly Design: Declared Optional OpenCode Worker

Add file:

```text
examples/polly/agents/opencode/config.yaml
```

Proposed spec:

```yaml
spec_version: 1
name: opencode
description: OpenCode coding sub-agent — implements, cross-vendor reviews, or explores a scoped task in its own worktree.

executor:
  type: omnigent
  config:
    harness: opencode-native
    # Permission mode field TBD after OpenCode permission mapping lands.
    # The goal is equivalent to Codex headless workers: do not block forever
    # on approval cards the orchestrator cannot answer.

prompt: |
  You are OpenCode, a coding sub-agent dispatched by the polly orchestrator for
  a single scoped task in a dedicated git worktree. Your task prompt names its
  purpose — IMPLEMENT, REVIEW, or EXPLORE. Do exactly that one thing; don't
  refactor or wander unprompted.

  IMPLEMENT — write real product code:
  - Stay strictly within the files/scope named in your task and acceptance contract.
  - Make the change, then drive it to green: run relevant tests, lint, and typecheck.
  - When green, push your task branch and open a PR. Never merge.

  REVIEW — verify another agent's diff:
  - Judge the diff only against the contract. Do not edit code.
  - Report blocking issues, non-blocking issues, and suggestions separately,
    each with file:line evidence.

  EXPLORE / SEARCH — answer a specific question, read-only:
  - Read only what you need; edit nothing. Answer with file:line evidence.
```

Update `examples/polly/config.yaml`:

- Change “exactly THREE sub-agents” (`examples/polly/config.yaml:59`) to “up to FOUR sub-agents”.
- Add roster item:

```text
- `opencode` — OpenCode (`opencode-native` harness), a native coding harness with terminal takeover support.
```

- Update preflight from:

```bash
command -v claude codex pi || true
```

which is currently documented at `examples/polly/config.yaml:70`, to:

```bash
command -v claude codex pi opencode || true
```

- Mark OpenCode optional: route to it only if the binary is present.
- Update dispatch rules to allow OpenCode for IMPLEMENT/REVIEW/EXPLORE.
- Update cross-review rules to consider OpenCode a fourth harness.

Default-optional behavior:

- Existing users without `opencode` binary should see no behavior change.
- Polly's prompt should tell the orchestrator to record OpenCode availability but not announce missing OpenCode unless asked.
- Dispatch layer will also fail loud if `opencode-native` is selected and CLI is missing after `onboarding/harness_install.py` maps it.

### D.3 Short-Term Debby Design: Optional Third Perspective

Debby is not a coding orchestrator; its existing identity is two heads: Claude and GPT. Do not change the default fanout.

Add optional file only if product wants OpenCode perspective:

```text
examples/debby/agents/opencode/config.yaml
```

Proposed role:

- OpenCode is an optional third perspective when explicitly requested.
- It is not used by default for every question.
- It can be useful for implementation-oriented brainstorming, codebase-grounded ideation, or “what would an OpenCode agent do?” comparisons.

Update `examples/debby/config.yaml`:

- Keep default two-head behavior at `examples/debby/config.yaml:47`.
- Add prompt section:

```text
Optional OpenCode perspective:
- You also have `opencode`, an optional OpenCode native coding-agent perspective.
- Do not dispatch it by default; Debby's default product promise remains Claude + GPT.
- Use it when the user explicitly asks for OpenCode, a coding-agent perspective,
  a three-way debate, or implementation-grounded critique.
```

Debby debate skill changes:

- Default `/debate` remains Claude/GPT.
- If user asks for three-way debate, include OpenCode and update round logic.
- Do not silently replace either existing head with OpenCode.

### D.4 Long-Term Design: `args.harness` with Allowlist

Add a runtime harness override to `sys_session_send`, but only for sub-agents that explicitly opt in.

#### Spec design

Option 1: executor config allowlist:

```yaml
executor:
  type: omnigent
  config:
    harness: codex-native
    allowed_harnesses:
      - codex-native
      - opencode-native
```

Option 2: top-level runtime variants:

```yaml
runtime_harnesses:
  default: codex-native
  allowed:
    - codex-native
    - opencode-native
```

Recommendation: use `executor.config.allowed_harnesses` first because harness selection belongs with executor config and can be validated near existing harness validation.

#### Tool schema change

Extend `args` object in `omnigent/tools/builtins/spawn.py`:

```json
"harness": {
  "type": "string",
  "description": "Optional harness override for this sub-agent session. Applies only at creation time and only when the sub-agent spec allowlists the harness."
}
```

Keep `additionalProperties: False`, but include `harness` as an allowed property.

#### Dispatch change

In `omnigent/runner/tool_dispatch.py`:

1. Parse `args.harness`.
2. Validate it is a string and canonicalize through `canonicalize_harness()`.
3. Find child sub-agent spec.
4. Validate child spec allowlist contains requested harness.
5. Validate requested harness is in `OMNIGENT_HARNESSES`.
6. Validate CLI availability with `missing_harness_cli()`.
7. Validate `args.model` against requested harness instead of default harness.
8. Add `harness_override` to `POST /v1/sessions` create body.

#### Server/session change

Today `Conversation.harness_override` docs say sub-agent sessions never inherit it (`omnigent/entities/conversation.py:124`). Update the invariant:

- Sub-agent sessions do not inherit parent brain `harness_override`.
- Sub-agent sessions may have their **own** create-time `harness_override` when dispatch provided an allowlisted override.

Server create route must enforce allowlist server-side, not only in runner tool dispatch, because sessions can be created through API paths too.

#### UI change

SubagentsPanel should show both:

- sub-agent type, e.g. `codex-reviewer`;
- effective harness, e.g. `OpenCode` if overridden.

### D.5 Cross-Vendor Review with OpenCode as Fourth Harness

Polly currently frames cross-review as independent different-vendor review (`examples/polly/config.yaml:46`). Adding OpenCode requires more precise metadata.

Dimensions:

| Dimension | Examples | Use |
|---|---|---|
| Harness | `claude-native`, `codex-native`, `pi`, `opencode-native` | Catches harness/tooling-specific bugs. |
| Model provider | Anthropic, OpenAI, Gemini, Databricks gateway | Catches model-family blind spots. |
| Model id | `claude-opus-*`, `gpt-*`, `gemini-*` | Strongest independence when different. |
| Runtime mode | native TUI, SDK, headless | Catches execution-mode differences. |

Policy recommendation:

- For security/correctness reviews, prefer different model provider.
- If different provider is unavailable, require at least different harness and disclose limitation.
- OpenCode using an OpenAI model should not count as a full different-vendor reviewer for Codex using an OpenAI model.
- OpenCode using Anthropic should not count as a full different-vendor reviewer for Claude Code using Anthropic.
- Pi remains useful as a model-flexible reviewer if it can run a different model family.

Prompt update for Polly:

```text
When choosing reviewers, track both harness and resolved model provider.
A reviewer must use a different model provider from the implementer when possible.
If you only have a different harness on the same provider, say so and treat the review
as weaker than a full cross-vendor review.
```

### D.6 Open Questions for Section D

1. Should OpenCode be enabled in Polly by default if installed, or only when the user opts in?
2. Should Debby include OpenCode at all, or should OpenCode remain coding-only?
3. Does `args.harness` expose too much control to orchestrator LLMs without a stricter allowlist?
4. How should model provider be surfaced from native harnesses into sub-agent result metadata?

---

## E. Unified Add-a-Harness Interface

### E.1 Problem

Adding OpenCode through today's architecture requires touching many unrelated files. This makes drift likely and makes future harness additions expensive.

A unified system should make a harness author implement:

1. One descriptor.
2. One executor or one native transport adapter.
3. Optional web/native metadata.
4. Conformance tests.

Everything else should be derived.

### E.2 `HarnessDescriptor` Dataclass

New file:

```text
omnigent/runtime/harness_descriptors.py
```

Proposed dataclass:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Protocol

HarnessFamily = Literal[
    "sdk",
    "native-server",
    "native-terminal",
    "headless-native",
    "legacy",
]

ModelIdFormat = Literal[
    "omnigent",
    "provider-slash-model",
    "native",
]

@dataclass(frozen=True)
class HarnessDescriptor:
    # Identity
    id: str
    display_name: str
    module: str
    aliases: tuple[str, ...] = ()
    family: HarnessFamily = "sdk"
    description: str | None = None

    # Runtime module / wrapper
    create_app_symbol: str = "create_app"
    executor_class: str | None = None
    package_extra: str | None = None

    # Capabilities
    supports_streaming: bool = True
    supports_tool_calling: bool = True
    handles_tools_internally: bool = False
    supports_interrupt: bool = False
    supports_enqueue: bool = False
    supports_terminal_takeover: bool = False
    supports_resume: bool = False
    supports_fork: bool = False
    supports_permissions: bool = False

    # Model behavior
    supports_model_override: bool = False
    model_id_format: ModelIdFormat = "omnigent"
    supported_model_families: tuple[str, ...] = ()
    default_model: str | None = None
    model_catalog_provider: str | None = None

    # CLI install/readiness
    cli_binary: str | None = None
    npm_package: str | None = None
    install_hint: str | None = None
    login_args: tuple[str, ...] | None = None
    logout_args: tuple[str, ...] | None = None
    status_args: tuple[str, ...] | None = None
    login_status_key: str | None = None
    min_cli_version: str | None = None
    max_cli_version_exclusive: str | None = None

    # Native UI / web
    wrapper_agent_name: str | None = None
    wrapper_label: str | None = None
    web_icon_kind: str | None = None
    web_sort_rank: int | None = None
    web_capabilities: tuple[str, ...] = ()
    terminal_first: bool = False

    # Native server transport
    transport_kind: str | None = None
    openapi_schema: str | None = None

    # Environment/readiness
    required_env: tuple[str, ...] = ()
    optional_env_prefixes: tuple[str, ...] = ()
    readiness_notes: tuple[str, ...] = ()

    # Extensibility metadata
    metadata: Mapping[str, object] = field(default_factory=dict)
```

### E.3 Single Registration Point

Same file:

```python
HARNESS_DESCRIPTORS: dict[str, HarnessDescriptor] = {
    "claude-sdk": HarnessDescriptor(...),
    "claude-native": HarnessDescriptor(...),
    "codex": HarnessDescriptor(...),
    "codex-native": HarnessDescriptor(...),
    "opencode-native": HarnessDescriptor(...),
    "pi": HarnessDescriptor(...),
    "pi-native": HarnessDescriptor(...),
    "cursor": HarnessDescriptor(...),
    "antigravity": HarnessDescriptor(...),
    "openai-agents": HarnessDescriptor(...),
    "open-responses": HarnessDescriptor(...),
}
```

OpenCode descriptor sketch:

```python
"opencode-native": HarnessDescriptor(
    id="opencode-native",
    display_name="OpenCode",
    module="omnigent.inner.opencode_native_harness",
    aliases=("native-opencode",),
    family="native-server",
    supports_streaming=True,
    handles_tools_internally=True,
    supports_interrupt=True,
    supports_enqueue=True,
    supports_terminal_takeover=True,
    supports_resume=True,
    supports_fork=True,
    supports_permissions=True,
    supports_model_override=True,
    model_id_format="provider-slash-model",
    cli_binary="opencode",
    npm_package="opencode-ai",  # verify published package; source package is "opencode"
    min_cli_version="1.17.7",
    max_cli_version_exclusive="1.18.0",
    wrapper_agent_name="opencode-native-ui",
    wrapper_label="opencode-native-ui",
    web_icon_kind="opencode",
    web_sort_rank=25,
    web_capabilities=("approvalMode",),
    terminal_first=True,
    transport_kind="http-sse",
    openapi_schema="opencode/openapi-1.17.7.json",
)
```

### E.4 Derived Views

Replace scattered registries with derived views:

| Existing view | Derived from descriptor |
|---|---|
| `_HARNESS_MODULES` | `{d.id: d.module for d in descriptors}` plus aliases if runtime accepts aliases. |
| `OMNIGENT_HARNESSES` | `frozenset(HARNESS_DESCRIPTORS)`. |
| `OMNIGENT_HARNESS_ALIASES` | union of `d.aliases`. |
| `HARNESS_ALIASES` | `{alias: d.id for d in descriptors for alias in d.aliases}`. |
| `NATIVE_HARNESSES` | descriptors where `family` starts with native or `terminal_first`. |
| `harness_supports_model_override()` | `descriptor.supports_model_override`. |
| model family mismatch | `descriptor.supported_model_families`. |
| `_HARNESS_INSTALL` | descriptors with `cli_binary` and install/login metadata. |
| `_HARNESS_NAME_TO_KEY` | direct id-to-descriptor rather than family indirection. |
| host readiness | descriptor readiness fields. |
| web native agents | descriptors where `wrapper_agent_name` and `terminal_first`. |
| built-in wrappers | descriptors where `wrapper_agent_name` exists. |
| optional extras | descriptors with `package_extra`. |

Migration plan:

1. Add descriptors and tests that assert descriptors match existing hard-coded views.
2. Switch one view at a time to derive from descriptors.
3. Remove duplicate hard-coded lists after all tests pass.

### E.5 `NativeServerTransport` Protocol

New file:

```text
omnigent/native_server_transport.py
```

Protocol:

```python
from typing import Any, AsyncIterator, Mapping, Protocol

@dataclass(frozen=True)
class NativeLaunchConfig:
    omnigent_session_id: str
    workspace: str
    model_override: str | None
    terminal_launch_args: tuple[str, ...]
    external_session_id: str | None
    server_url: str | None
    auth_headers: Mapping[str, str]

@dataclass(frozen=True)
class NativeServerHandle:
    base_url: str
    env: Mapping[str, str]
    bridge_dir: Path
    process_id: int | None = None

@dataclass(frozen=True)
class NativePrompt:
    text: str
    attachments: tuple[Mapping[str, Any], ...] = ()
    system_prompt: str | None = None
    model: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class NativeEvent:
    id: str | None
    type: str
    payload: Mapping[str, Any]
    raw: Mapping[str, Any]

@dataclass(frozen=True)
class NativePermissionDecision:
    request_id: str
    decision: Literal["allow_once", "allow_always", "reject"]
    message: str | None = None

class NativeServerTransport(Protocol):
    descriptor_id: str

    async def start_server(self, launch: NativeLaunchConfig) -> NativeServerHandle: ...
    async def stop_server(self) -> None: ...
    async def create_or_resume_session(self, launch: NativeLaunchConfig) -> str: ...
    async def send_prompt(self, session_id: str, prompt: NativePrompt) -> Mapping[str, Any]: ...
    async def abort(self, session_id: str) -> bool: ...
    async def events(self, session_id: str) -> AsyncIterator[NativeEvent]: ...
    async def list_history(self, session_id: str) -> list[Mapping[str, Any]]: ...
    async def fork(self, session_id: str, *, at_message_id: str | None = None) -> str: ...
    async def reply_permission(self, decision: NativePermissionDecision) -> None: ...
    def build_tui_attach_command(self, launch: NativeLaunchConfig, session_id: str) -> tuple[list[str], Mapping[str, str]]: ...
```

### E.6 `CodexWsTransport` vs `OpenCodeHttpTransport`

| Method | `CodexWsTransport` | `OpenCodeHttpTransport` |
|---|---|---|
| `start_server` | Launch `codex app-server --listen ws/unix`. | Launch `opencode serve --hostname 127.0.0.1 --port P`. |
| `create_or_resume_session` | `thread/resume` or new thread. | `GET /session/{id}` or `POST /session`. |
| `send_prompt` | JSON-RPC `turn/start` or `turn/steer`. | REST `POST /session/{id}/prompt_async` or `/message`. |
| `abort` | JSON-RPC `turn/interrupt`. | REST `POST /session/{id}/abort`. |
| `events` | WebSocket JSON-RPC notifications. | SSE `GET /event`. |
| `list_history` | Codex rollout/session files or app-server history. | REST `GET /session/{id}/message`. |
| `fork` | Clone/rebuild rollout or resume new thread. | REST `POST /session/{id}/fork`. |
| `reply_permission` | Codex elicitation resolution. | REST `POST /permission/{requestID}/reply`. |
| `build_tui_attach_command` | `codex --remote <url> ...`. | `opencode attach <url> --dir W --session S`. |

### E.7 `NativeServerHarness` Base

New file:

```text
omnigent/native_server_harness.py
```

Base class:

```python
class NativeServerHarness(Executor):
    def __init__(
        self,
        *,
        descriptor: HarnessDescriptor,
        transport: NativeServerTransport,
        bridge_store: NativeBridgeStore,
        event_mapper: NativeEventMapper,
    ) -> None: ...

    def supports_streaming(self) -> bool:
        return self.descriptor.supports_streaming

    def handles_tools_internally(self) -> bool:
        return self.descriptor.handles_tools_internally

    async def run_turn(self, messages, tools, system_prompt, config=None):
        launch = self._launch_config(config)
        handle = await self.transport.start_server(launch)
        native_session_id = await self.transport.create_or_resume_session(launch)
        await self._ensure_forwarder(handle, native_session_id)
        prompt = self._build_prompt(messages, system_prompt, config)
        await self.transport.send_prompt(native_session_id, prompt)
        yield TurnComplete(response=None)

    async def interrupt_session(self, session_key: str) -> bool:
        native_session_id = self.bridge_store.native_session_id(session_key)
        return await self.transport.abort(native_session_id)

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        ...

    async def close_session(self, session_key: str) -> None:
        await self.transport.stop_server()
```

The base should own:

- Launch config normalization.
- Bridge state load/write.
- Forwarder task lifecycle.
- External session id persistence callback.
- Generic interrupt/queue behavior.
- Generic permission decision round-trip.
- Generic TUI command exposure to runner.

Transport-specific code should own only protocol details.

### E.8 Harness Mapping Table

| Harness | Current type | Future descriptor family | NativeServerHarness? | Notes |
|---|---|---|---|---|
| `opencode-native` | New native server | `native-server` | Yes, `OpenCodeHttpTransport` | First new implementation. |
| `codex-native` | Native server + TUI | `native-server` | Eventually, `CodexWsTransport` | Migrate after OpenCode proves abstraction. |
| `claude-native` | Native terminal wrapper | `native-terminal` | Not initially | No app-server equivalent; descriptor still helps registries. |
| `pi-native` | Native terminal/headless bridge | `native-terminal` or `headless-native` | Not initially | Shares terminal/readiness but not server transport. |
| `pi` | Headless native or SDK-like executor | `headless-native` | No | Descriptor handles install/model. |
| `cursor` | Python SDK executor | `sdk` | No | Descriptor handles optional extra/readiness. |
| `antigravity` | Python SDK executor | `sdk` | No | Descriptor handles optional extra/readiness. |
| `claude-sdk` | Python SDK executor | `sdk` | No | Descriptor handles model/provider. |
| `openai-agents` | Python SDK executor | `sdk` | No | Descriptor handles model/provider. |
| `open-responses` | Python SDK/API executor | `sdk` | No | Descriptor handles model/provider. |

### E.9 Conformance Test Suite

Add `tests/harness_conformance/`.

Tests:

1. `test_descriptors_are_complete.py`
   - Every descriptor has id, display name, module, family.
   - Every descriptor id is canonical and not an alias.

2. `test_runtime_registry_derives_from_descriptors.py`
   - `_HARNESS_MODULES` equals descriptor modules.
   - Every module imports and has `create_app`.

3. `test_spec_allowlist_derives_from_descriptors.py`
   - `OMNIGENT_HARNESSES == frozenset(descriptors)`.
   - All aliases canonicalize to descriptor ids.

4. `test_native_flags_match_descriptors.py`
   - `is_native_harness()` agrees with descriptor family/terminal fields.

5. `test_model_override_support_matches_descriptors.py`
   - `harness_supports_model_override()` agrees with descriptor field.

6. `test_install_metadata_matches_descriptors.py`
   - CLI-backed descriptors have `cli_binary` plus `npm_package` or `install_hint`.

7. `test_web_native_registry_matches_descriptors.py`
   - Generated or checked TS registry includes all descriptors with `wrapper_agent_name`.

8. `test_native_server_transport_contract.py`
   - Fake `NativeServerTransport` passes run_turn, abort, forwarder, permission tests.

### E.10 Scaffold Generator

Optional developer CLI:

```bash
omnigent dev scaffold-harness opencode-native --family native-server --transport http-sse
```

Generated files:

- `omnigent/inner/<name>_harness.py`
- `omnigent/inner/<name>_executor.py`
- `omnigent/<name>_bridge.py`
- `omnigent/<name>_forwarder.py`
- descriptor stub
- test stubs
- web metadata stub if native UI requested

Generator should also print checklist items from Appendix A.

---

## Sequencing and PR Breakdown

### Recommendation: OpenCode First, Then Unify

OpenCode should land before the full unified interface extraction.

Reasons:

- OpenCode has real unknowns: event payloads, permission schema, model pinning, auth, and v1/v2 API boundaries.
- Extracting a generic `NativeServerHarness` before implementing a second transport risks overfitting to Codex.
- A working OpenCode implementation will reveal the correct shared boundary between native server lifecycle, transport, forwarder, permissions, and terminal attach.
- Descriptor unification can start early as a no-behavior-change safety net, but the full migration should follow OpenCode core.

### Concrete PR List

#### PR 1 — Descriptor Skeleton and Drift Tests

- Add `HarnessDescriptor` with descriptors for existing harnesses.
- Add conformance tests that compare descriptors to current hard-coded registries.
- No behavior change.

#### PR 2 — OpenCode Client and Fake Server Tests

- Add `opencode_native_client.py`.
- Vendor or fixture OpenAPI schema for pinned `1.17.7`.
- Add fake HTTP/SSE server tests.
- Add version parsing tests.

#### PR 3 — OpenCode Core Headless Harness

- Add bridge state, app-server manager, executor, harness wrapper.
- Register `opencode-native` in runtime/spec/install/readiness.
- Implement prompt injection, abort, basic forwarder text/status.
- No TUI yet.

#### PR 4 — Permissions, Model Pinning, Resume/Fork

- Add permission mapping.
- Add model override plumbing.
- Persist `external_session_id`.
- Implement fork/resume best effort.
- Add integration tests.

#### PR 5 — OpenCode TUI Terminal Takeover

- Add `_auto_create_opencode_terminal()`.
- Add `opencode attach` command builder and env handling.
- Keep forwarder live while terminal attached.
- Add terminal resource tests and gated real CLI smoke test.

#### PR 6 — AP Web OpenCode Native UI

- Add OpenCode native registry entry.
- Add card/display/icon/setup behavior.
- Add SubagentsPanel support.
- Add web tests.

#### PR 7 — Polly Optional OpenCode Worker

- Add `examples/polly/agents/opencode/config.yaml`.
- Update Polly prompt/preflight/cross-review semantics.
- Add docs.
- Add parser/tool-dispatch smoke tests.

#### PR 8 — Debby Optional OpenCode Perspective

- Add optional Debby OpenCode worker only if product wants it.
- Keep default Claude/GPT behavior.
- Update debate skill for explicit three-way debate.

#### PR 9 — NativeServerHarness Extraction

- Extract `NativeServerTransport` and `NativeServerHarness`.
- Move OpenCode onto it first if not already built that way.
- Gradually adapt Codex Native behind compatibility wrappers.

#### PR 10 — Derived Registries

- Replace hard-coded registries with descriptor-derived views.
- Generate or serve web native metadata.
- Strengthen conformance tests to prevent future drift.

---

## Risk Register

| Risk | Severity | Likelihood | Mitigation |
|---|---:|---:|---|
| OpenCode API instability during v1/v2 migration | High | High | Pin CLI/API version; vendor OpenAPI fixture; version-check at startup. |
| Published npm package name mismatch (`opencode-ai` vs source `opencode`) | Medium | Medium | Verify package before implementation; keep install metadata easy to patch. |
| SSE event payloads differ from assumptions | High | Medium | Capture real fixtures; forwarder tolerates unknown events; implement conservative mapping. |
| Permission mapping misses dangerous tool path | High | Medium | Fail closed for unknown permission types; add policy fixtures; require manual approval until mapped. |
| OpenCode model pin cannot be set via REST | Medium | Medium | Use per-session config; if unavailable, mark model override unsupported initially. |
| Attach TUI leaks auth secret in argv | Medium | Low | Prefer env `OPENCODE_SERVER_PASSWORD`; never pass password on argv by default. |
| Web and TUI concurrent prompts race | High | Medium | Track busy/idle; queue or reject web prompts while TUI is active. |
| Duplicate transcript items | Medium | High | Dedupe by OpenCode session/message/part ids. |
| Per-session XDG dirs break auth/config | Medium | Medium | Inject provider env from Omnigent setup; document OpenCode login fallback. |
| OpenCode server accidentally exposed beyond runner | High | Low | Force `--hostname 127.0.0.1`; random port; auth secret. |
| Polly routes to missing OpenCode CLI | Low | Medium | Prompt preflight plus dispatch `missing_harness_cli()` check. |
| Cross-vendor review semantics become misleading | Medium | Medium | Track harness and model provider separately; disclose weak same-provider reviews. |
| Descriptor unification regresses existing harnesses | High | Medium | Descriptor-only PR first; conformance tests; migrate one view at a time. |
| TUI attach behavior changes in future OpenCode | Medium | Medium | Version pin; fallback to headless mode; test attach command in readiness. |
| Large OpenCode tool outputs overload UI | Low | Medium | Reuse Omnigent output caps; summarize huge outputs. |
| OpenCode forks not portable across hosts | Medium | Medium | Prefer same-host fork API; fallback to Omnigent transcript rebuild. |

---

## Full Test Matrix

### Unit Tests

| Area | Tests |
|---|---|
| Client | create session; get session; list messages; prompt; prompt_async; abort; fork; list permissions; reply permission; SSE parse; auth headers; directory/workspace routing. |
| Server manager | executable resolution; version parsing; argv includes explicit host/port; env includes XDG dirs; auth env set; readiness polling; shutdown. |
| Bridge state | prepare dir; read/write state; corrupted JSON recovery; external session id update; auth secret file permissions. |
| Executor | first run starts server; creates session; starts forwarder; injects prompt; yields `TurnComplete`; interrupt calls abort; busy prompt queues; close stops server. |
| Forwarder | translates text part delta; message part update; tool running/completed/error; permission request; session idle completion; unknown events; reconnect; dedupe. |
| Permissions | once/always/reject mapping; policy auto-allow; policy auto-deny; user approval reply; duplicate request dedupe. |
| Model pinning | model override accepted; model in payload/config; invalid family rejected if implemented; no silent drop. |
| Resume/fork | resume existing session id; missing native session fallback; fork endpoint used; fork fallback rebuild. |

### Harness Conformance Tests

| Test | Purpose |
|---|---|
| Descriptor completeness | Every harness has descriptor fields. |
| Runtime registry parity | `_HARNESS_MODULES` matches descriptors. |
| Spec allowlist parity | `OMNIGENT_HARNESSES` matches descriptors. |
| Alias parity | aliases canonicalize to descriptor ids. |
| Native parity | `is_native_harness()` agrees with descriptors. |
| Model override parity | `harness_supports_model_override()` agrees with descriptors. |
| CLI install parity | CLI-backed harnesses have install/readiness metadata. |
| Web registry parity | Native web agents match native descriptors. |

### Spawn-Env Tests

| Scenario | Expected result |
|---|---|
| `opencode` missing | Readiness reports missing; dispatch fails loud with install hint. |
| OpenCode installed unsupported version | Harness startup fails with actionable upgrade/downgrade message. |
| Provider env configured | `opencode serve` gets provider env. |
| Proxy env configured | `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` propagated. |
| Auth secret configured | Serve and attach share env secret. |
| Per-session XDG dirs | OpenCode writes under bridge dir, not global user state. |

### Readiness Tests

| Area | Expected result |
|---|---|
| Host configured harnesses | Includes OpenCode when binary/version/auth checks pass. |
| Web setup badge | Shows “needs setup” when OpenCode missing. |
| CLI install command | Suggests correct npm package or install hint. |
| Login status | Uses OpenCode-supported status command if available; otherwise binary-only readiness until auth check exists. |

### Per-Harness E2E Tests

Gated behind `OPENCODE_E2E=1` and requiring real `opencode`:

1. Start Omnigent session with `opencode-native`.
2. Send prompt: “Say hello and do not modify files.”
3. Observe assistant response streamed into Omnigent.
4. Send long-running prompt and interrupt; verify `POST /abort` behavior.
5. Trigger a permission prompt with a command/file action; approve once; observe continuation.
6. Attach TUI with `opencode attach`; type a prompt; verify web transcript mirrors it.
7. Restart runner; resume same OpenCode session id; attach TUI again.
8. Fork session; verify OpenCode fork or fallback behavior.

### Web E2E Tests

| Scenario | Expected result |
|---|---|
| New Chat OpenCode card | Card appears as “OpenCode”; creates terminal-first session. |
| Missing setup | Card or harness row shows setup warning. |
| Terminal takeover | Clicking terminal opens OpenCode TUI resource. |
| Polly OpenCode sub-agent | Subagents panel shows `opencode` child and terminal. |
| Child navigation | Opening child session lands in terminal-first view. |
| Model change | Model picker/update shows correct behavior or limitation. |

### Polly/Debby Tests

| Agent | Test |
|---|---|
| Polly | Spec parser discovers `opencode` worker. |
| Polly | Preflight includes `opencode`. |
| Polly | `sys_session_send(agent="opencode")` creates child with `sub_agent_name="opencode"`. |
| Polly | Missing CLI returns OpenCode-specific install hint. |
| Polly | Cross-review prompt requires different provider where possible. |
| Debby | Default still dispatches only `claude` and `gpt`. |
| Debby | Explicit OpenCode request dispatches optional `opencode`. |
| Debby | Three-way debate includes OpenCode only when requested. |

---

## Appendix A: Extension-Point Checklist

To add `opencode-native` before descriptor unification:

- [ ] Add `omnigent/inner/opencode_native_harness.py`.
- [ ] Add `omnigent/inner/opencode_native_executor.py`.
- [ ] Add `omnigent/opencode_native_app_server.py`.
- [ ] Add `omnigent/opencode_native_client.py`.
- [ ] Add `omnigent/opencode_native_bridge.py`.
- [ ] Add `omnigent/opencode_native_forwarder.py`.
- [ ] Add `omnigent/opencode_native_state.py`.
- [ ] Register in `_HARNESS_MODULES` (`omnigent/runtime/harnesses/__init__.py`).
- [ ] Add to `OMNIGENT_HARNESSES` (`omnigent/spec/_omnigent_compat.py`).
- [ ] Add aliases/native recognition if desired (`omnigent/harness_aliases.py`).
- [ ] Add model override support if implemented (`omnigent/model_override.py`).
- [ ] Add model catalog provider mapping (`omnigent/model_catalog.py`).
- [ ] Add CLI install metadata (`omnigent/onboarding/harness_install.py`).
- [ ] Add readiness checks (`omnigent/onboarding/harness_readiness.py`).
- [ ] Add built-in wrapper spec and seeding (`omnigent/server/app.py`).
- [ ] Add wrapper label constant (`omnigent/_wrapper_labels.py`).
- [ ] Add runner auto terminal creation (`omnigent/runner/app.py`).
- [ ] Add Web native agent registry entry (`ap-web/src/lib/nativeCodingAgents.ts`).
- [ ] Add Web labels if brain override allowed (`ap-web/src/lib/agentLabels.ts`).
- [ ] Add SubagentsPanel icon/name support.
- [ ] Add tests for all above.

To add a future harness after descriptor unification:

- [ ] Add one `HarnessDescriptor`.
- [ ] Implement `Executor` or `NativeServerTransport`.
- [ ] Add fake transport/client tests.
- [ ] Add optional built-in wrapper spec if terminal-first.
- [ ] Run conformance tests.

---

## Appendix B: Landing-PR References and Review Notes

### Existing implementation references

- Harness scaffold contract: `omnigent/runtime/harnesses/_scaffold.py:5`.
- Executor interface: `omnigent/inner/executor.py:495`.
- ExecutorAdapter: `omnigent/runtime/harnesses/_executor_adapter.py:137`.
- Codex Native harness wrapper: `omnigent/inner/codex_native_harness.py:1`.
- Codex app-server launch: `omnigent/codex_native_app_server.py:560`.
- Codex remote TUI attach: `omnigent/codex_native_app_server.py:1511`.
- Codex turn injection: `omnigent/inner/codex_native_executor.py:216`.
- Runtime harness registry: `omnigent/runtime/harnesses/__init__.py:34`.
- Spec harness allowlist: `omnigent/spec/_omnigent_compat.py:76`.
- Alias/native harness helpers: `omnigent/harness_aliases.py:9`, `omnigent/harness_aliases.py:26`.
- CLI install metadata: `omnigent/onboarding/harness_install.py:99`, `omnigent/onboarding/harness_install.py:140`.
- Native web registry: `ap-web/src/lib/nativeCodingAgents.ts:21`.
- Brain harness labels: `ap-web/src/lib/agentLabels.ts:8`.
- New Chat harness picker: `ap-web/src/shell/NewChatDialog.tsx:639`.
- New Chat session create `harness_override`: `ap-web/src/shell/NewChatDialog.tsx:1200`.
- Polly config: `examples/polly/config.yaml:59`.
- Debby config: `examples/debby/config.yaml:42`.
- `sys_session_send` schema: `omnigent/tools/builtins/spawn.py:242`.
- Sub-agent dispatch harness resolution: `omnigent/runner/tool_dispatch.py:800`.

### OpenCode implementation references

- OpenCode package version/bin: `/tmp/opencode/packages/opencode/package.json:3`, `/tmp/opencode/packages/opencode/package.json:18`.
- OpenCode CLI package version: `/tmp/opencode/packages/cli/package.json:3`, `/tmp/opencode/packages/cli/package.json:4`.
- Serve command: `/tmp/opencode/packages/opencode/src/cli/cmd/serve.ts:6`.
- Network options: `/tmp/opencode/packages/opencode/src/cli/network.ts:6`.
- Attach command: `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:7`.
- Attach auth flags: `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:35`, `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:40`.
- Attach TUI run args: `/tmp/opencode/packages/opencode/src/cli/cmd/attach.ts:82`.
- TUI model option: `/tmp/opencode/packages/opencode/src/cli/cmd/tui.ts:80`.
- TUI internal/external transport branch: `/tmp/opencode/packages/opencode/src/cli/cmd/tui.ts:148`.
- Session route paths: `/tmp/opencode/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:80`.
- Session create endpoint: `/tmp/opencode/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:203`.
- Session abort endpoint: `/tmp/opencode/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:253`.
- Event bridge envelope: `/tmp/opencode/packages/opencode/src/event-v2-bridge.ts:42`.
- Server connected event: `/tmp/opencode/packages/opencode/src/server/event.ts:5`.
- Message part updated event: `/tmp/opencode/packages/core/src/v1/session.ts:615`.

### Review notes for first implementation PR

- Keep OpenCode launch isolated to loopback and per-session XDG dirs.
- Do not claim model override support until verified end-to-end.
- Do not claim permission policy parity until `once`/`always`/`reject` are fixture-tested.
- Keep OpenCode unknown SSE events visible in debug logs but non-fatal.
- Avoid refactoring Codex Native in the OpenCode landing PR.
- Add descriptor skeleton early if it reduces drift, but avoid blocking OpenCode on full unification.
