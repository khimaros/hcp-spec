# hcp wire contract

normative reference for the harness control protocol. the
[README](README.md) introduces the model; this document specifies the
wire format, base payload, and every stage's payload and response.
keywords MUST / SHOULD / MAY follow RFC 2119.

## protocol invocation

the host invokes each hook script as:

```
<script> <stage>
```

- `argv[1]` is the stage name.
- stdin is a single JSON object terminated by EOF.
- stdout is JSONL — each line is a JSON object.
- stderr is forwarded to the host's debug log.
- exit code 0 = success; non-zero = failure.

hosts MUST treat `EPIPE` on the stdin write as success: a hook MAY exit
before consuming stdin.

scripts whose basename starts with `.` or `__` MUST be ignored.
discovery order is alphabetical.

## base payload

every stage's stdin payload includes at minimum:

- `hook` — the stage name
- `session` — `{"id": "..."}`. one-shot CLIs use a synthetic id.
- `host` — `{"name": "...", "version": 2, "stages": [...]}` advertising
  which stages this host actually fires (discovery only, no
  negotiation)

stage-specific fields are listed under each stage below.

## output framing

each line of stdout MUST be a valid JSON object. recognized keys are
merged into the hook result; lines with `{"log": "..."}` are routed to
the host's debug log and not merged.

## composability

multiple hook scripts run serially in alphabetical order. results merge
across all scripts:

- arrays (`system`, `tools`, …) are concatenated
- scalars (`continue`, `prompt`, `user`, `message`, `result`, `text`)
  are joined with newline
- a script's failure is independent: other scripts continue regardless

## tier 0 — universal

### `discover`

declarative setup. the only stage that registers capabilities. fires
once per script at startup before any other stage.

**payload:** base fields only.

**response:**
- `name` — short prefix used to namespace registered tools (defaults
  to the script's file stem)
- `stages` — stages the script handles (advisory; helps hosts skip
  invocations that would no-op)
- `tools` — tool definitions exposed to the LLM as `<prefix>_<short>`.
  see [§ tool parameter types](#tool-parameter-types).

### `mutate_request`

fires once before the conversation loop starts. the system prompt has
been composed and the user prompt has been read. hooks see the full
request and can mutate or short-circuit.

**payload:**
- `system` — finalized system prompt
- `user` — user prompt
- `model` — provider/model string
- `tools` — tool definitions about to be sent to the LLM (host MAY
  omit if the underlying API does not expose this at the relevant
  point)

**response:**
- `system` — array of strings appended to the system prompt
- `cancel` — `{reason: "..."}` to skip the LLM call entirely
- `result` — synthetic assistant text to emit when `cancel` is set

### `before_tool`

about to run a tool. fires for built-in tools and hook-registered tools
alike.

**payload:**
- `tool` — tool name
- `call_id` — unique id for this call
- `args` — parsed arguments
- `turn` — 0-indexed turn this call belongs to
- `pattern_matched` — the permission glob that authorized this call

**response:**
- `deny` — `{reason: "..."}` to refuse the call; the model receives
  the reason as the tool result
- `args` — replacement arguments (redaction, defaults)
- `result` — synthetic result substituting for actual execution
  (caching, mocking)

### `after_tool`

tool finished. the hook sees the result before it goes back to the
model and can mutate it.

**payload:**
- `tool` — tool name
- `call_id` — matches `before_tool`'s `call_id`
- `args` — final args used (after any `before_tool` rewrite)
- `result` — result string from the tool
- `duration_ms` — wall-clock execution time
- `turn` — the turn this call belonged to

**response:**
- `result` — replacement result text fed back to the model

### `execute_tool`

implements a hook-registered tool. only fires for tools the script
declared in `discover`.

**payload:**
- `tool` — short tool name (without the script prefix)
- `args` — parsed arguments

**response:**
- `result` — string returned to the model (json-encoded objects are
  conventional but not required)

## tier 1 — loop-aware

### `before_turn`

fires before each LLM round-trip. turn 0 is the initial request; turn
1+ are tool-result follow-ups.

**payload:**
- `turn` — 0-indexed turn number
- `history` — message history about to be sent
- `tools` — tools available this turn

**response:**
- `system` — extra system messages injected for this turn only
- `tools` — replacement tool list (filter or modify what the model
  sees)
- `skip` — `{reason: "..."}` to skip this turn (loop advances as if
  the model said "stop")

### `after_turn`

LLM has responded. the hook sees the assistant message before any
tool calls dispatch.

**payload:**
- `turn` — 0-indexed turn number
- `assistant` — `{text, reasoning?, tool_calls: [...]}`
- `usage` — per-turn token usage if available

**response:**
- `stop` — `{reason: "..."}` to terminate the loop after this turn,
  even if tool calls were issued
- `text` — replacement for the rendered assistant text

### `before_stop`

the conversation loop is about to terminate. fires regardless of *why*
(natural stop, max_turns, error, cancel) so logging-only hooks always
see the final state. continuation hooks can re-enter the loop.

**payload:**
- `transcript` — full message history (system, user, assistant turns,
  tool calls, tool results)
- `exit_reason` — one of `stop`, `max_turns`, `error`, `cancel`
- `error` — error string when `exit_reason = error`
- `usage` — `{input_tokens, output_tokens}` if available
- `final` — `true` when the host will not honor `continue` (e.g.
  max_turns hit, fatal error). hooks can still observe but `continue`
  is ignored.

**response:**
- `continue` — string injected as a synthetic user turn; loop re-enters
  if `final` was not set

### `on_error`

fires on a runtime error during the loop (network failure, provider
5xx, parse error, tool execution failure). observational; useful for
retry policies and external alerting.

**payload:**
- `phase` — `request`, `stream`, `tool`, `parse`
- `error` — error message
- `recoverable` — whether the host will retry / continue

**response:** ignored.

## tier 2 — interception

### `on_permission`

fires when a tool call hits an `ask` permission decision and the host
is about to prompt the user. lets policy hooks auto-decide based on
context the static config cannot see.

**payload:**
- `tool` — tool name
- `args` — parsed arguments
- `pattern_matched` — the glob pattern that resolved to `ask`

**response:**
- `decision` — `allow`, `ask`, or `deny`. `ask` (or omitted) falls
  through to the interactive prompt. `allow` / `deny` short-circuit
  it.
- `reason` — surfaced to the user when the hook short-circuits

## tool parameter types

tools registered via `discover.tools` declare their parameters in the
`parameters` field. three forms are accepted:

- **string shorthand** — `{"arg": "description"}` declares a string
  parameter with the given description.
- **typed** — `{"arg": {"type": "string", "description": "...",
  "optional": true}}`.
- **enum** — `{"arg": {"type": "string", "enum": ["a", "b"],
  "description": "..."}}`. the host MUST reject invalid values before
  reaching `execute_tool`. `type` is implicitly string for enums.

supported core types: `string`, `number`, `boolean`, `object`, `array`,
`any`. `optional: true` marks the parameter as not required. hosts MAY
support extended forms (e.g. element-typed arrays) but scripts targeting
multiple hosts SHOULD stay within the core set.

`object` produces a JSON Schema with unconstrained values
(`additionalProperties: {}`); there is no per-field schema. the LLM
relies on the description string to understand the expected shape, so
clear examples and explicit "do not wrap" language matter for nested
shapes.

`permission.arg` (optional) on the tool definition names the parameter
whose value is used as the permission pattern key. tools without
`permission` use pattern `"*"`.

## what's deliberately not here

- **streaming chunk hooks** — per-token volume makes shell hooks
  impractical; `after_turn` covers the same need at coarser
  granularity.
- **`after_run` as a separate stage** — folded into `before_stop`.

hosts MAY define additional stages, additional payload fields, and
additional response keys on top of the protocol. the `host.stages`
field of the base payload is the discovery mechanism. host-specific
extensions are out of scope for this spec and belong in the host's own
documentation.
