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
- stdout is JSONL: each line is a JSON object.
- stderr is forwarded to the host's debug log.
- exit code 0 = success; non-zero = failure.

hosts MUST treat `EPIPE` on the stdin write as success: a hook MAY exit
before consuming stdin.

scripts whose basename starts with `.` or `__` MUST be ignored.
discovery order is alphabetical.

## base payload

every stage's stdin payload includes at minimum:

- `hook`: the stage name
- `session`: `{"id": "..."}`. one-shot CLIs use a synthetic id.
- `host`: `{"name": "...", "version": 3, "stages": [...]}` advertising
  which stages this host actually fires (discovery only, no
  negotiation). `version` is per-host; a host advertises the protocol
  version it implements (currently 3).
- `cwd` (v3): the workspace root. a hook reads its own files from here
  (prompts, data, state); the host reads NO workspace file except the
  hook scripts it discovers under `<cwd>/hooks`. a hook that needs
  prompts, config, or persistent state owns and reads them itself. hosts
  before v3 omit `cwd`; a hook may derive the root from its own path.

> **v3 note — prompts are hook-owned.** earlier hosts (pi-evolve,
> opencode-evolve) loaded a `prompts/` contract and injected it into every
> stage as `ctx.prompts`. under v3 the host injects no prompt contract; a
> hook reads its own prompt files from `cwd`. a host MAY still freeze the
> composed system prompt per session for provider prompt-cache stability
> (that is a host concern, orthogonal to who composes the text).

stage-specific fields are listed under each stage below.

## output framing

each line of stdout MUST be a valid JSON object. recognized keys are
merged into the hook result; lines with `{"log": "..."}` are routed to
the host's debug log and not merged.

## composability

multiple hook scripts run serially in alphabetical order. results merge
across all scripts:

- arrays (`system`, `tools`, ...) are concatenated
- scalars (`continue`, `prompt`, `user`, `message`, `result`, `text`)
  are joined with newline
- a script's failure is independent: other scripts continue regardless

## tier 0: universal

### `discover`

declarative setup. the only stage that registers capabilities. fires
once per script at startup before any other stage.

**payload:** base fields only.

**response:**
- `name`: short prefix used to namespace registered tools (defaults
  to the script's file stem)
- `stages`: stages the script handles (advisory; helps hosts skip
  invocations that would no-op). a host MAY use it to arm only the
  interception hooks a script actually handles.
- `tools`: tool definitions exposed to the LLM as `<prefix>_<short>`.
  see [tool parameter types](#tool-parameter-types).
- `test` (optional): a command (relative to `cwd`) the host runs to
  validate the hook before installing an edit to it (the recoverable
  self-edit path). e.g. `"hello_test.py"`.

a hook does NOT declare a heartbeat cadence here: the host owns scheduling
end to end (see [heartbeat](#heartbeat)). a hook opts into beats simply by
handling the `heartbeat` stage (and MAY list it in `stages`).

### `mutate_request`

fires once before the conversation loop starts. the system prompt has
been composed and the user prompt has been read. hooks see the full
request and can mutate or short-circuit.

**payload:**
- `system`: finalized system prompt
- `user`: user prompt
- `model`: provider/model string
- `tools`: tool definitions about to be sent to the LLM (host MAY
  omit if the underlying API does not expose this at the relevant
  point)

**response:**
- `system`: array of strings contributed to the system prompt
- `system_mode`: how `system` composes with the host default.
  `"append"` (default, back-compatible) appends `system` after the
  host default, keeping that default as a cache-stable prefix;
  `"replace"` makes `system` the hook's prompt and drops the host
  default prose. a host that injects a model-invocable skills catalog
  into its system prompt MUST carry that catalog across `replace`
  (dropping it would silently revoke the model's skills); the catalog
  is session-stable, so it stays inside the frozen result. the host
  freezes the composed result per session either way, so `replace`
  is at least as prompt-cache-stable as `append`.
- `cancel`: `{reason: "..."}` to skip the LLM call entirely
- `result`: synthetic assistant text to emit when `cancel` is set

### `before_tool`

about to run a tool. fires for built-in tools and hook-registered tools
alike.

**payload:**
- `tool`: tool name
- `call_id`: unique id for this call
- `args`: parsed arguments
- `turn`: 0-indexed turn this call belongs to
- `pattern_matched`: the permission glob that authorized this call

**response:**
- `deny`: `{reason: "..."}` to refuse the call; the model receives
  the reason as the tool result
- `args`: replacement arguments (redaction, defaults)
- `result`: synthetic result substituting for actual execution
  (caching, mocking)

### `after_tool`

tool finished. the hook sees the result before it goes back to the
model and can mutate it.

**payload:**
- `tool`: tool name
- `call_id`: matches `before_tool`'s `call_id` (canonical spelling;
  hooks SHOULD also tolerate the historical `callID`)
- `args`: final args used (after any `before_tool` rewrite)
- `result`: result string from the tool. a host MAY carry it under
  `output` instead; a hook SHOULD accept either.
- `duration_ms`: wall-clock execution time
- `turn`: the turn this call belonged to

**response:**
- `result`: replacement result text fed back to the model

### `execute_tool`

implements a hook-registered tool. only fires for tools the script
declared in `discover`.

**payload:**
- `tool`: short tool name (without the script prefix)
- `args`: parsed arguments

**response:**
- `result`: string returned to the model (json-encoded objects are
  conventional but not required)

## tier 1: loop-aware

### `before_turn`

fires before each LLM round-trip. turn 0 is the initial request; turn
1+ are tool-result follow-ups.

**payload:**
- `turn`: 0-indexed turn number
- `history`: message history about to be sent
- `tools`: tools available this turn

**response:**
- `system`: extra system messages injected for this turn only
- `tools`: replacement tool list (filter or modify what the model
  sees)
- `skip`: `{reason: "..."}` to skip this turn (loop advances as if
  the model said "stop")

### `after_turn`

LLM has responded. the hook sees the assistant message before any
tool calls dispatch.

**payload:**
- `turn`: 0-indexed turn number
- `assistant`: `{text, reasoning?, tool_calls: [...]}`
- `usage`: per-turn token usage if available

**response:**
- `stop`: `{reason: "..."}` to terminate the loop after this turn,
  even if tool calls were issued
- `text`: replacement for the rendered assistant text

### `before_stop`

the conversation loop is about to terminate. fires regardless of *why*
(natural stop, max_turns, error, cancel) so logging-only hooks always
see the final state. continuation hooks can re-enter the loop.

**payload:**
- `transcript`: full message history (system, user, assistant turns,
  tool calls, tool results)
- `exit_reason`: one of `stop`, `max_turns`, `error`, `cancel`
- `error`: error string when `exit_reason = error`
- `usage`: `{input_tokens, output_tokens}` if available
- `final`: `true` when the host will not honor `continue` (e.g.
  max_turns hit, fatal error). hooks can still observe but `continue`
  is ignored.

**response:**
- `continue`: string injected as a synthetic user turn; loop re-enters
  if `final` was not set

### `on_error`

fires on a runtime error during the loop (network failure, provider
5xx, parse error, tool execution failure). observational; useful for
retry policies and external alerting.

**payload:**
- `phase`: `request`, `stream`, `tool`, `parse`
- `error`: error message
- `recoverable`: whether the host will retry / continue

**response:** ignored.

## tier 2: interception

### `on_permission`

fires when a tool call hits an `ask` permission decision and the host
is about to prompt the user. lets policy hooks auto-decide based on
context the static config cannot see.

**payload:**
- `tool`: tool name
- `args`: parsed arguments
- `pattern_matched`: the glob pattern that resolved to `ask`

**response:**
- `decision`: `allow`, `ask`, or `deny`. `ask` (or omitted) falls
  through to the interactive prompt. `allow` / `deny` short-circuit
  it.
- `reason`: surfaced to the user when the hook short-circuits

## tier 3: host extensions

stages beyond the LLM loop that a host with autonomous / notification /
recovery machinery fires. they are optional: a host advertises the ones it
fires in `host.stages`, and a hook degrades gracefully when a stage never
fires. these were informal host extensions (pi-evolve / opencode-evolve)
before v3; they are specified here so identical hook scripts run across hosts.

### `heartbeat`

fires on a recurring cadence the host schedules, in a dedicated session, so
an agent can act autonomously between user turns. the host drives one turn
with the returned prompt. the host owns scheduling end to end -- WHETHER to
beat, the cadence, the durable config, and the run history; a hook opts in
simply by handling this stage (and MAY list `heartbeat` in `discover.stages`).
the hook is stateless, and the host MAY pass recent history to this stage so
the beat can see what it has already done.

**payload:** base fields (the `session` id is the host's heartbeat session),
plus optional `history`: recent beat records the host has logged (below).

**response:**
- `system`: array of strings, the system prompt for the heartbeat turn
- `user`: the user-role prompt that drives the turn (empty / omitted skips
  this beat)

**control surface (host-owned).** the schedule, the durable config, and the
run history are all HOST state. a host that fires `heartbeat` SHOULD expose
them to the agent as host-provided tools:
- a **set** tool taking `{ every_secs?, enabled? }`: retunes the live timer
  AND persists it, so the change survives a restart. `enabled: false` (or
  `every_secs: 0`) disables the beat; a later `enabled: true` restores it.
- a **status** tool reporting `{ enabled, every_secs, next_run_at,
  last_run_at, runs, source }` -- `source` says whether the current cadence
  is the agent's persisted choice or a deployment default.
- a **history** tool returning the last N beat records, each at least
  `{ started_at, ended_at, ok }`.

**durability + precedence.** the host persists `{ enabled, every_secs }`
across restarts (storage host-defined) and restores its timer from that on
startup. the effective config resolves highest priority first:
1. a host deployment override that force-disables (e.g. an env flag) --
   always wins, for eval/demo.
2. the agent's persisted runtime state, once it has set one.
3. the deployment default cadence (host config).

with none of these set, the host does not fire a heartbeat. the hook declares
nothing about scheduling, so identical hook scripts get durable control on
any conforming host.

### `observe_message`

fires after each assistant message, so a hook can react to what was said
(update state, queue a notification). observational: the loop does not wait
on it.

**payload:** `session`, plus the assistant `answer` / tool calls the host
exposes.

**response:** `modified` / `notify` / `actions` the host applies (see the
host's own docs); a bare `{}` is the no-op default.

### `format_notification`

fires when the host has queued notifications (e.g. from a `notify` response
key) to render them for the people who should hear about them.

**payload:**
- `notifications`: the queued notification objects
- `sessions`: OPTIONAL. the sessions this notice could be delivered to, so a
  hook can choose an audience without having to ask for one. each entry
  carries at least `id`, and where the host knows them: `title`, `agent`,
  `harness_id`, `updated_at`, and `identity` -- the bare id of the human who
  has SPOKEN in that session. a host that does not model people omits it.

**response:**
- `message`: the user-facing text (empty / omitted suppresses it)
- `to`: OPTIONAL. who should receive it. ABSENT means everyone the host would
  normally tell, which is the behaviour a hook that ignores this key keeps.
  present, it names an audience:
  - `sessions`: a list of session ids
  - `identity`: a bare identity id; matches sessions that person has spoken in

  selectors UNION rather than intersect ("these sessions, and wherever this
  person is talking"). an audience that names nobody -- `{}` -- reaches
  nobody: absence and emptiness are deliberately different, because getting
  that backwards would broadcast exactly the notices meant to be private.

each hook's notice is its own notice. a host MUST NOT merge the `message` of
several hooks into one before delivering it: they may have different
audiences, and joining them also welds two unrelated sentences into one.

### `recover`

fires when another stage in the host's recover set throws, so a hook can
re-enter cleanly instead of the turn dying.

**payload:**
- `failed_hook`: the stage that threw
- `error`: the error message

**response:** `system` / `user`, a synthetic re-entry the host injects.

### `compacting`

fires before the host compacts (summarizes) a session's context, so a hook can
supply its own summarization instructions instead of the backend's default.

**payload:**
- `prompt`: the current instructions (any user-supplied compaction request), which
  the hook may replace
- read-only context the backend provides (e.g. `history`)

**response:**
- `prompt`: the summarization instructions the backend runs the compaction with.
  an empty/absent `prompt` means the host keeps its DEFAULT compaction -- the
  fallback a host without a compacting hook (or one that abstains) always takes.

## tool parameter types

tools registered via `discover.tools` declare their parameters in the
`parameters` field. three forms are accepted:

- **string shorthand**: `{"arg": "description"}` declares a string
  parameter with the given description.
- **typed**: `{"arg": {"type": "string", "description": "...",
  "optional": true}}`.
- **enum**: `{"arg": {"type": "string", "enum": ["a", "b"],
  "description": "..."}}`. the host MUST reject invalid values before
  reaching `execute_tool`. `type` is implicitly string for enums.

supported core types: `string`, `number`, `boolean`, `object`, `array`,
`any`. `optional: true` marks the parameter as not required. hosts MAY
support extended forms (e.g. element-typed arrays) but scripts targeting
multiple hosts SHOULD stay within the core set.

`optional` describes the CONTRACT, not one wire form. a host MAY serialize
it either way when it builds the model's tool schema: omitted from
`required`, or listed in `required` with a `null` arm (`anyOf: [T,
{"type": "null"}]`). the second is what a host must emit if it asks its
provider to constrain sampling to the schema -- strict JSON Schema has no
way to spell "may be omitted" -- and it is worth allowing, since a tool
schema comes from a third party and an argument the host cannot validate
is a failure with nothing to tune. both forms mean the same thing to the
script: the model passes `null` where it would otherwise have omitted the
key, and `execute_tool` sees an absent value. conformance accepts either
and distinguishes them, so a host may NOT use the null arm to quietly make
a required parameter optional.

`object` produces a JSON Schema with unconstrained values
(`additionalProperties: {}`); there is no per-field schema. the LLM
relies on the description string to understand the expected shape, so
clear examples and explicit "do not wrap" language matter for nested
shapes.

`permission.arg` (optional) on the tool definition names the parameter
whose value is used as the permission pattern key. tools without
`permission` use pattern `"*"`.

## what's deliberately not here

- **streaming chunk hooks**: per-token volume makes shell hooks
  impractical; `after_turn` covers the same need at coarser
  granularity.
- **`after_run` as a separate stage**: folded into `before_stop`.

hosts MAY define additional stages, additional payload fields, and
additional response keys on top of the protocol. the `host.stages`
field of the base payload is the discovery mechanism. host-specific
extensions are out of scope for this spec and belong in the host's own
documentation.
