"""End-to-end tests for the generated pi-native bridge extension."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_delivery_cap_drops_followup_without_failed_session_status(
    tmp_path: Path,
) -> None:
    """The extension must not terminal-fail a session when follow-up delivery caps.

    This runs the real JavaScript extension under Node with a real inbox payload
    and mocked Pi/fetch boundaries. Five consecutive ``sendUserMessage`` throws
    should consume the inbox file and emit an informational conversation item,
    never ``external_session_status`` with ``status: "failed"``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension e2e test")

    extension_path = (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )

    script = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const inboxDir = path.join(tmpDir, "inbox");
const payloadPath = path.join(inboxDir, "000-msg.json");
const configPath = path.join(tmpDir, "config.json");

fs.mkdirSync(inboxDir, { recursive: true });
fs.writeFileSync(
  payloadPath,
  JSON.stringify({ id: "msg-1", type: "user_message", content: "follow up" }),
);
fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://omnigent.test",
    sessionId: "session-1",
    inboxDir,
    authHeaders: { authorization: "Bearer test" },
  }),
);

process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

const postedEvents = [];
global.fetch = async (_url, request) => {
  postedEvents.push(JSON.parse(request.body));
  return { ok: true };
};

let pollInbox = null;
global.setInterval = (fn, _ms) => {
  pollInbox = fn;
  return { fakeInterval: true };
};

const handlers = {};
const sendAttempts = [];
const pi = {
  registerCommand() {},
  on(eventName, handler) {
    handlers[eventName] = handler;
  },
  sendUserMessage(content, options) {
    sendAttempts.push({ content, options });
    throw new Error("Pi is not ready");
  },
};

require(extensionPath)(pi);

(async () => {
  assert.equal(typeof handlers.session_start, "function");
  await handlers.session_start({}, {
    sessionManager: { getSessionId: () => "native-session-1" },
    ui: { setTitle() {}, setStatus() {}, notify() {} },
  });
  assert.equal(typeof pollInbox, "function");

  for (let attempt = 0; attempt < 5; attempt += 1) {
    pollInbox();
  }
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(
    sendAttempts,
    Array.from({ length: 5 }, () => ({
      content: "follow up",
      options: { deliverAs: "followUp" },
    })),
  );
  assert.equal(fs.existsSync(payloadPath), false);
  assert.equal(
    postedEvents.some(
      (event) =>
        event.type === "external_session_status" &&
        event.data &&
        event.data.status === "failed",
    ),
    false,
    JSON.stringify(postedEvents),
  );

  const dropNote = postedEvents.find(
    (event) =>
      event.type === "external_conversation_item" &&
      event.data &&
      event.data.item_type === "error" &&
      event.data.item_data &&
      event.data.item_data.code === "pi_followup_delivery_dropped",
  );
  assert.ok(dropNote, JSON.stringify(postedEvents));
  assert.equal(dropNote.data.item_data.source, "execution");
  assert.match(dropNote.data.response_id, /^pi-deliver-dropped-/);
  // The note must be actionable: include the dropped message id and a preview
  // of its content so an operator can identify what was lost.
  assert.match(dropNote.data.item_data.message, /msg-1/);
  assert.match(dropNote.data.item_data.message, /follow up/);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""

    result = subprocess.run(
        [node, "-e", script, str(extension_path), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# ── TOOL_CALL policy evaluation (DENY / ALLOW / ASK elicitation) ──────────
#
# These exercise the real evalNativePolicyHttp park/resolve loop in the
# generated extension by driving its tool_call handler under Node with a
# scripted fetch and assert-rich JS body. Each case supplies a queue of
# responses; the JS harness drives one tool_call and reports the verdict.

# Shared JS preamble: loads the extension, wires a scripted fetch + fake
# timers (so the long park / backoff budgets collapse to instant in test),
# and exposes runToolCall() which fires the tool_call handler and returns the
# verdict the extension would hand back to Pi.
_POLICY_HARNESS_PREAMBLE = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const configPath = path.join(tmpDir, "config.json");

fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://omnigent.test",
    sessionId: "session-1",
    authHeaders: { authorization: "Bearer test" },
  }),
);
process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

// Captured evaluate-request bodies (parsed) in call order.
const evalBodies = [];
// Queue of responders. Each entry is a function (parsedBody) -> response-like
// object, OR the string "THROW" to simulate a transport error, OR
// "THROW_ABORT" to simulate our own AbortController firing (DOMException-ish).
let responders = [];
let evalCallCount = 0;

function makeJsonResponse(obj, status) {
  return {
    ok: status === undefined || (status >= 200 && status < 300),
    status: status === undefined ? 200 : status,
    json: async () => obj,
  };
}

global.fetch = async (url, request) => {
  const body = JSON.parse(request.body);
  // Only the evaluate endpoint is scripted; postEvent calls (events endpoint)
  // just succeed silently so they never interfere with the verdict assertions.
  if (typeof url === "string" && url.includes("/policies/evaluate")) {
    evalBodies.push(body);
    const idx = evalCallCount;
    evalCallCount += 1;
    const responder = responders[idx];
    if (responder === "THROW") {
      throw new Error("ECONNREFUSED simulated transport error");
    }
    if (responder === "THROW_ABORT") {
      // Simulate our own AbortController firing after holding the connection
      // for the FULL per-attempt park timeout — i.e. a legitimate long-poll
      // re-attach. The extension disambiguates a real park from a genuine
      // transport error by the attempt's elapsed wall-time (a real connect
      // error throws fast; a real park only aborts at the per-attempt
      // timeout), so advance the fake clock by the per-attempt budget before
      // flipping the signal and throwing the AbortError fetch would raise.
      fakeNow += PARK_ATTEMPT_TIMEOUT_MS;
      if (request && request.signal && typeof request.signal._abort === "function") {
        request.signal._abort();
      }
      const err = new Error("The operation was aborted");
      err.name = "AbortError";
      throw err;
    }
    if (typeof responder === "function") return responder(body);
    // Default: allow.
    return makeJsonResponse({ result: "POLICY_ACTION_ALLOW" });
  }
  return { ok: true, status: 200, json: async () => ({}) };
};

// Fake clock + timers. A short delay (sleep/backoff) advances a virtual clock
// by its duration and fires on the next microtask, so the extension's
// wall-clock budgets (transient retry, park ceiling) elapse deterministically
// and instantly — no real 30s wait. The long park abort timer (>= 100s) is
// never fired so a scripted fetch always resolves first; but scheduling it
// still advances nothing (it is cleared in the finally).
let fakeNow = 1_000_000;
// Mirrors _PARK_ATTEMPT_TIMEOUT_MS in the extension. A real long-poll abort
// only fires after the connection is held this long; the THROW_ABORT responder
// advances the fake clock by this amount so the extension classifies it as a
// legitimate re-attach (vs. a fast-failing genuine transport error).
const PARK_ATTEMPT_TIMEOUT_MS = 240000;
const realDateNow = Date.now.bind(Date);
Date.now = () => fakeNow;
global.setTimeout = (fn, ms) => {
  if (typeof ms === "number" && ms >= 100000) {
    return { fakeBig: true };
  }
  if (typeof ms === "number" && ms > 0) fakeNow += ms;
  Promise.resolve().then(fn);
  return { fakeSmall: true };
};
global.clearTimeout = () => {};
// Keep the inbox poller dormant.
global.setInterval = () => ({ fakeInterval: true });

const handlers = {};
const pi = {
  registerCommand() {},
  on(eventName, handler) {
    handlers[eventName] = handler;
  },
  sendUserMessage() {},
};

require(extensionPath)(pi);

const ctx = {
  sessionManager: { getSessionId: () => "native-session-1" },
  ui: { setTitle() {}, setStatus() {}, notify() {} },
  abort() {},
  isIdle() { return false; },
};

async function runToolCall() {
  assert.equal(typeof handlers.tool_call, "function");
  return handlers.tool_call(
    { toolCallId: "call-1", toolName: "Bash", input: { command: "rm -rf /tmp/x" } },
    ctx,
  );
}

// Captured external_conversation_item events posted to the /events endpoint
// (the conversation mirror). Lets a tool_result test assert the mirrored
// output reflects what the model actually consumed (suppressed on DENY).
const mirroredItems = [];
const _baseFetch = global.fetch;
global.fetch = async (url, request) => {
  if (typeof url === "string" && url.includes("/events")) {
    try {
      mirroredItems.push(JSON.parse(request.body));
    } catch (_e) {}
    return { ok: true, status: 200, json: async () => ({}) };
  }
  return _baseFetch(url, request);
};

// Fire the tool_result handler with a realistic Pi ToolResultEvent: a content
// array (TextContent blocks), the original tool input, and the tool name. The
// returned value is what Pi applies to the finalized tool result the model
// sees (undefined = unchanged; {content, isError} = replaced/suppressed).
async function runToolResult(resultText) {
  assert.equal(typeof handlers.tool_result, "function");
  return handlers.tool_result(
    {
      type: "tool_result",
      toolCallId: "call-1",
      toolName: "Bash",
      input: { command: "cat secrets.txt" },
      content: [{ type: "text", text: resultText }],
      isError: false,
    },
    ctx,
  );
}
"""


def _run_policy_node_script(extension_path: Path, tmp_path: Path, body: str) -> None:
    """Run the extension's tool_call policy path under Node with a scripted fetch.

    :param extension_path: Path to the generated extension JS.
    :param tmp_path: Per-test scratch dir (config is written here).
    :param body: JS test body appended after the shared harness preamble; it
        sets ``responders`` and runs assertions inside an async IIFE.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension policy test")

    script = _POLICY_HARNESS_PREAMBLE + "\n" + body
    result = subprocess.run(
        [node, "-e", script, str(extension_path), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _extension_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )


def test_policy_allow_proceeds(tmp_path: Path) -> None:
    """An ALLOW verdict lets the Pi tool call proceed (no block returned)."""
    body = r"""
(async () => {
  responders = [(_b) => makeJsonResponse({ result: "POLICY_ACTION_ALLOW" })];
  const verdict = await runToolCall();
  // tool_call returns undefined (or a non-blocking value) on ALLOW.
  assert.ok(!verdict || verdict.block !== true, JSON.stringify(verdict));
  assert.equal(evalBodies.length, 1);
  // The evaluate body must carry a valid re-attach id and a PHASE_TOOL_CALL.
  assert.match(evalBodies[0]._omnigent_elicitation_id, /^elicit_evaluate_[0-9a-f]{32}$/);
  assert.equal(evalBodies[0].event.type, "PHASE_TOOL_CALL");
  assert.equal(evalBodies[0].event.data.name, "Bash");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_deny_blocks(tmp_path: Path) -> None:
    """A DENY verdict blocks the Pi tool call and surfaces the policy reason."""
    body = r"""
(async () => {
  responders = [
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_DENY", reason: "no rm -rf" }),
  ];
  const verdict = await runToolCall();
  assert.deepEqual(verdict, { block: true, reason: "no rm -rf" });
  assert.equal(evalBodies.length, 1);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_ask_parks_then_resolves_allow(tmp_path: Path) -> None:
    """A raw ASK re-evaluates (re-attaching) until it resolves to ALLOW.

    The first evaluate returns ASK (gate did not park server-side); the loop
    re-POSTs the SAME elicitation id and the second returns ALLOW, so the tool
    call proceeds. Mirrors the server collapsing ASK to a hard verdict.
    """
    body = r"""
(async () => {
  responders = [
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_ASK", reason: "approve?" }),
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_ALLOW" }),
  ];
  const verdict = await runToolCall();
  assert.ok(!verdict || verdict.block !== true, JSON.stringify(verdict));
  assert.equal(evalBodies.length, 2, "expected park-then-resolve = 2 evaluates");
  // Both POSTs must reuse the SAME elicitation id so the server re-attaches
  // rather than opening a second approval card.
  assert.equal(
    evalBodies[0]._omnigent_elicitation_id,
    evalBodies[1]._omnigent_elicitation_id,
  );
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_ask_parks_then_resolves_deny(tmp_path: Path) -> None:
    """A raw ASK that resolves to DENY blocks the Pi tool call with the reason."""
    body = r"""
(async () => {
  responders = [
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_ASK", reason: "approve?" }),
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_DENY", reason: "declined" }),
  ];
  const verdict = await runToolCall();
  assert.deepEqual(verdict, { block: true, reason: "declined" });
  assert.equal(evalBodies.length, 2);
  assert.equal(
    evalBodies[0]._omnigent_elicitation_id,
    evalBodies[1]._omnigent_elicitation_id,
  );
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_park_abort_reattaches_same_id(tmp_path: Path) -> None:
    """An aborted park (our own headers-timeout guard) re-attaches, not re-mints.

    Simulates undici severing a long park: the first attempt throws an
    AbortError (signal.aborted), the loop must re-POST the SAME elicitation id
    immediately (no backoff), and the resolved ALLOW lets the tool proceed.
    """
    body = r"""
(async () => {
  // First call: pretend our AbortController fired (the fake controller below
  // reports aborted=true). Second call: resolved ALLOW.
  responders = ["THROW_ABORT", (_b) => makeJsonResponse({ result: "POLICY_ACTION_ALLOW" })];
  // Fake AbortController whose signal exposes a private _abort() so the fetch
  // mock can flip aborted=true (mimicking our 240s headers-timeout guard
  // firing). The extension's catch reads controller.signal.aborted to choose
  // the re-attach (no-backoff) branch over the transient-error branch.
  global.AbortController = class {
    constructor() {
      let aborted = false;
      const signal = {};
      Object.defineProperty(signal, "aborted", { get() { return aborted; } });
      signal._abort = () => { aborted = true; };
      this.signal = signal;
    }
    abort() { this.signal._abort(); }
  };
  const verdict = await runToolCall();
  assert.ok(!verdict || verdict.block !== true, JSON.stringify(verdict));
  assert.equal(evalBodies.length, 2, "expected re-attach after abort = 2 evaluates");
  assert.equal(
    evalBodies[0]._omnigent_elicitation_id,
    evalBodies[1]._omnigent_elicitation_id,
  );
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_transport_error_fails_closed(tmp_path: Path) -> None:
    """A persistent transport error fails CLOSED (finding #1).

    PHASE_TOOL_CALL is the sole enforcement point for a native connector tool,
    so an unevaluable policy must BLOCK, not proceed. Every evaluate POST
    throws a non-abort transport error; after the transient retry budget
    elapses the extension returns a deny verdict (fail closed) rather than
    null, matching omnigent.policies.types.FAIL_CLOSED_PHASES and the Python
    native hook's fail_closed_hook_output(PreToolUse) → deny.
    """
    body = r"""
(async () => {
  // Always throw a transport error; with fake timers collapsing the backoff
  // the transient budget elapses quickly and the loop must fail CLOSED.
  responders = new Array(64).fill("THROW");
  const verdict = await runToolCall();
  // Fail closed → a block verdict with the unreachable-server reason.
  assert.equal(verdict && verdict.block, true, JSON.stringify(verdict));
  assert.match(verdict.reason, /unreachable/);
  assert.match(verdict.reason, /failing closed/);
  // It must have actually retried a few times before giving up (not one-shot).
  assert.ok(evalBodies.length >= 2, "expected retries, got " + evalBodies.length);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_server_5xx_fails_closed(tmp_path: Path) -> None:
    """A persistent 5xx response fails CLOSED once the transient budget is spent.

    A 5xx is transient (retried, re-attaching), but if it never clears the
    gate cannot produce a verdict, so PHASE_TOOL_CALL fails closed rather than
    proceeding.
    """
    body = r"""
(async () => {
  responders = new Array(64).fill((_b) => makeJsonResponse({}, 503));
  const verdict = await runToolCall();
  assert.equal(verdict && verdict.block, true, JSON.stringify(verdict));
  assert.match(verdict.reason, /failing closed/);
  assert.ok(evalBodies.length >= 2, "expected retries, got " + evalBodies.length);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_raw_ask_never_collapses_fails_closed(tmp_path: Path) -> None:
    """A raw ASK that never collapses fails CLOSED after the round cap (finding #2).

    A read-only caller's gate can return ASK without parking server-side. The
    extension re-evaluates (re-attaching the same id), but a raw ASK that NEVER
    collapses to a hard verdict must not ride the 24h park ceiling and then
    proceed — after _MAX_RAW_ASK_ROUNDS it denies, mirroring the Python native
    hook's stray-ASK-closed behavior.
    """
    body = r"""
(async () => {
  // Always ASK — it never collapses to ALLOW/DENY.
  const askForever = (_b) =>
    makeJsonResponse({ result: "POLICY_ACTION_ASK", reason: "approve?" });
  responders = new Array(256).fill(askForever);
  const verdict = await runToolCall();
  // Fail closed → a block verdict, NOT a proceed (the old 24h-hang-then-allow).
  assert.equal(verdict && verdict.block, true, JSON.stringify(verdict));
  assert.match(verdict.reason, /failing closed/);
  // It re-attached several rounds before giving up, but is bounded (not 24h /
  // unbounded). All POSTs reuse the SAME elicitation id.
  assert.ok(evalBodies.length >= 2, "expected re-evaluation, got " + evalBodies.length);
  assert.ok(evalBodies.length <= 50, "raw ASK rounds must be capped, got " + evalBodies.length);
  const ids = new Set(evalBodies.map((b) => b._omnigent_elicitation_id));
  assert.equal(ids.size, 1, "all raw-ASK rounds must reuse one elicitation id");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_fast_error_racing_abort_is_bounded_fails_closed(
    tmp_path: Path,
) -> None:
    """A genuine error racing the abort timer is bounded, not infinite (finding #3).

    Once the per-attempt abort timer has fired, controller.signal.aborted reads
    true even for a real connect reset that raced it. The old code re-attached
    on ``aborted`` alone, so such an error retried unboundedly toward the 24h
    ceiling. The fix disambiguates by elapsed wall-time: a fast-failing error
    (well under the per-attempt timeout) is a genuine transport error charged
    against the short transient budget — so a persistent one fails CLOSED
    instead of looping forever.

    THROW_FAST_ABORT flips ``aborted`` true (as if the timer had fired) but
    throws IMMEDIATELY (no clock advance), modelling the race.
    """
    body = r"""
(async () => {
  // Fake AbortController whose signal can be flipped aborted=true.
  global.AbortController = class {
    constructor() {
      let aborted = false;
      const signal = {};
      Object.defineProperty(signal, "aborted", { get() { return aborted; } });
      signal._abort = () => { aborted = true; };
      this.signal = signal;
    }
    abort() { this.signal._abort(); }
  };
  // Every attempt: flip aborted=true (timer "fired") but throw immediately with
  // NO clock advance — a genuine reset racing the abort timer. The extension
  // must charge these against the transient budget (elapsed ~= 0 << per-attempt
  // timeout) and ultimately fail CLOSED, not re-attach forever.
  let calls = 0;
  responders = new Array(512).fill(null).map(() => (_b) => {
    throw new Error("__USE_FAST_ABORT__");
  });
  // Override fetch's THROW path is awkward; instead simulate via a responder
  // that flips the (current) controller's signal then throws. The controller is
  // recreated each attempt, so reach it through the request signal.
  global.fetch = async (url, request) => {
    const parsed = JSON.parse(request.body);
    if (typeof url === "string" && url.includes("/policies/evaluate")) {
      evalBodies.push(parsed);
      calls += 1;
      if (request && request.signal && typeof request.signal._abort === "function") {
        request.signal._abort(); // timer "fired" — aborted=true
      }
      const err = new Error("ECONNRESET racing abort");
      err.name = "AbortError";
      throw err; // immediate: elapsed ~= 0, NOT a real long-poll
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  const verdict = await runToolCall();
  // Bounded by the transient budget → fail CLOSED, not an infinite re-attach.
  assert.equal(verdict && verdict.block, true, JSON.stringify(verdict));
  assert.match(verdict.reason, /failing closed/);
  // Retried a few times (transient budget) but nowhere near unbounded.
  assert.ok(evalBodies.length >= 2, "expected transient retries, got " + evalBodies.length);
  assert.ok(evalBodies.length < 200, "must be bounded, got " + evalBodies.length);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


# ── TOOL_RESULT policy evaluation (ALLOW / DENY / ASK / fail-open) ────────
#
# These drive the extension's tool_result handler (the second enforcement
# checkpoint, SKILL.md #6). Unlike tool_call, the result handler returns a Pi
# ToolResultEventResult ({content, isError}) that REPLACES what the model sees:
#   - ALLOW  → returns undefined (model gets the real output), result mirrored.
#   - DENY   → returns {content:[policy error], isError:true} (model gets the
#              policy error, real output suppressed), suppressed text mirrored.
#   - ASK    → parked server-side, collapses to ALLOW/DENY (same as tool_call).
#   - server unreachable → fail OPEN (result returned unchanged), because the
#              tool already executed (PHASE_TOOL_RESULT is NOT fail-closed).


def test_tool_result_allow_returns_unchanged(tmp_path: Path) -> None:
    """An ALLOW TOOL_RESULT verdict returns the real output to the model.

    The handler returns undefined (no replacement) so Pi hands the model the
    unmodified result, and the result is mirrored once with its real output.
    The evaluate body must be a PHASE_TOOL_RESULT carrying the result text and
    the originating tool name in request_data.
    """
    body = r"""
(async () => {
  responders = [(_b) => makeJsonResponse({ result: "POLICY_ACTION_ALLOW" })];
  const out = await runToolResult("normal safe output");
  // ALLOW → no replacement object (model keeps the real result).
  assert.ok(out === undefined || out === null, JSON.stringify(out));
  assert.equal(evalBodies.length, 1);
  assert.equal(evalBodies[0].event.type, "PHASE_TOOL_RESULT");
  assert.equal(evalBodies[0].event.data.result, "normal safe output");
  assert.equal(evalBodies[0].event.request_data.name, "Bash");
  assert.match(evalBodies[0]._omnigent_elicitation_id, /^elicit_evaluate_[0-9a-f]{32}$/);
  // Mirrored once with the REAL output.
  const mirror = mirroredItems.find(
    (e) => e.data && e.data.item_type === "function_call_output",
  );
  assert.ok(mirror, JSON.stringify(mirroredItems));
  assert.equal(mirror.data.item_data.output, "normal safe output");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_tool_result_deny_suppresses_and_returns_policy_error(
    tmp_path: Path,
) -> None:
    """A DENY TOOL_RESULT verdict suppresses the result and errors to the model.

    The handler must return {content:[<policy error>], isError:true} so Pi
    replaces the real output with the policy error before the model consumes
    it. The real (sensitive) output must NOT appear in the replacement, and the
    mirror must show the SAME suppressed text (what the model actually saw).
    """
    body = r"""
(async () => {
  responders = [
    (_b) =>
      makeJsonResponse({ result: "POLICY_ACTION_DENY", reason: "leaked SECRET" }),
  ];
  const out = await runToolResult("here is the SECRET api key sk-123");
  // DENY → replacement content + isError so the model gets the policy error.
  assert.ok(out && Array.isArray(out.content), JSON.stringify(out));
  assert.equal(out.isError, true);
  const replaced = out.content.map((c) => c.text).join("");
  assert.match(replaced, /policy/i);
  assert.match(replaced, /leaked SECRET/);
  // The real sensitive output must be GONE from what the model receives.
  assert.equal(replaced.includes("sk-123"), false, replaced);
  assert.equal(evalBodies.length, 1);
  assert.equal(evalBodies[0].event.type, "PHASE_TOOL_RESULT");
  // The mirror reflects the suppressed text, not the real output.
  const mirror = mirroredItems.find(
    (e) => e.data && e.data.item_type === "function_call_output",
  );
  assert.ok(mirror, JSON.stringify(mirroredItems));
  assert.equal(mirror.data.item_data.output.includes("sk-123"), false);
  assert.match(mirror.data.item_data.output, /policy/i);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_tool_result_ask_parks_then_resolves_allow(tmp_path: Path) -> None:
    """A raw TOOL_RESULT ASK re-attaches until it resolves to ALLOW.

    Mirrors the tool-call gate: the server normally parks a result-phase ASK
    and returns a hard verdict, but if a raw ASK comes back the extension
    re-POSTs the SAME elicitation id and an eventual ALLOW returns the result
    to the model unchanged.
    """
    body = r"""
(async () => {
  responders = [
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_ASK", reason: "review?" }),
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_ALLOW" }),
  ];
  const out = await runToolResult("output pending review");
  assert.ok(out === undefined || out === null, JSON.stringify(out));
  assert.equal(evalBodies.length, 2, "expected park-then-resolve = 2 evaluates");
  assert.equal(
    evalBodies[0]._omnigent_elicitation_id,
    evalBodies[1]._omnigent_elicitation_id,
  );
  assert.equal(evalBodies[0].event.type, "PHASE_TOOL_RESULT");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_tool_result_ask_parks_then_resolves_deny_suppresses(
    tmp_path: Path,
) -> None:
    """A TOOL_RESULT ASK that resolves to DENY suppresses the result.

    The parked ASK collapses to DENY (the human declined); the handler must
    then suppress the output and return the policy error to the model.
    """
    body = r"""
(async () => {
  responders = [
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_ASK", reason: "review?" }),
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_DENY", reason: "declined" }),
  ];
  const out = await runToolResult("sensitive output");
  assert.ok(out && Array.isArray(out.content), JSON.stringify(out));
  assert.equal(out.isError, true);
  const replaced = out.content.map((c) => c.text).join("");
  assert.match(replaced, /declined/);
  assert.equal(replaced.includes("sensitive output"), false, replaced);
  assert.equal(evalBodies.length, 2);
  assert.equal(
    evalBodies[0]._omnigent_elicitation_id,
    evalBodies[1]._omnigent_elicitation_id,
  );
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_tool_result_transport_error_fails_open(tmp_path: Path) -> None:
    """A persistent transport error on TOOL_RESULT fails OPEN, not closed.

    PHASE_TOOL_RESULT is NOT in FAIL_CLOSED_PHASES: by the result phase the
    tool already executed, so an unevaluable policy must return the result
    (fail open) rather than suppress a legitimately-produced output on a
    transient outage. The handler must return undefined (no suppression) even
    though the server was unreachable, and still mirror the real output.
    """
    body = r"""
(async () => {
  // Always throw a transport error; with fake timers collapsing the backoff
  // the transient budget elapses quickly. TOOL_RESULT must fail OPEN.
  responders = new Array(64).fill("THROW");
  const out = await runToolResult("real output survives outage");
  // Fail OPEN → no replacement; the model keeps the real result.
  assert.ok(out === undefined || out === null, JSON.stringify(out));
  // It actually retried before giving up (not one-shot).
  assert.ok(evalBodies.length >= 2, "expected retries, got " + evalBodies.length);
  // The real output is still mirrored (not suppressed).
  const mirror = mirroredItems.find(
    (e) => e.data && e.data.item_type === "function_call_output",
  );
  assert.ok(mirror, JSON.stringify(mirroredItems));
  assert.equal(mirror.data.item_data.output, "real output survives outage");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_tool_result_raw_ask_never_collapses_fails_open(tmp_path: Path) -> None:
    """A raw TOOL_RESULT ASK that never collapses fails OPEN after the cap.

    Bounded re-attach like the tool-call gate, but because PHASE_TOOL_RESULT
    fails open the bounded give-up returns the result unchanged rather than
    suppressing it.
    """
    body = r"""
(async () => {
  const askForever = (_b) =>
    makeJsonResponse({ result: "POLICY_ACTION_ASK", reason: "review?" });
  responders = new Array(256).fill(askForever);
  const out = await runToolResult("output that never gets reviewed");
  // Fail OPEN → no suppression.
  assert.ok(out === undefined || out === null, JSON.stringify(out));
  assert.ok(evalBodies.length >= 2, "expected re-evaluation, got " + evalBodies.length);
  assert.ok(evalBodies.length <= 50, "raw ASK rounds must be capped, got " + evalBodies.length);
  const ids = new Set(evalBodies.map((b) => b._omnigent_elicitation_id));
  assert.equal(ids.size, 1, "all raw-ASK rounds must reuse one elicitation id");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)
