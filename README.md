# hcp: harness control protocol

a host-agnostic contract for plugging external lifecycle hooks into a
coding-agent harness. shared across multiple harnesses
([hrns](https://github.com/khimaros/hrns),
[pi-evolve](https://github.com/khimaros/pi-evolve),
[opencode-evolve](https://github.com/khimaros/opencode-evolve)); identical
hook scripts run unchanged against any of them.

the design is deliberately minimal. the host forks a subprocess for
each event, writes a JSON request to its stdin, and reads JSONL from
its stdout. there is no long-running daemon, shared library, or
language binding, so any executable that can read stdin and write
stdout qualifies as a hook.

## terms

- **host**: the agent harness embedding the protocol.
- **hook script**: an executable file the host invokes per lifecycle
  event.
- **stage**: a named lifecycle event (`mutate_request`, `before_tool`,
  `before_stop`, ...).

## shape

the host invokes each hook as `<script> <stage>` with a single JSON
object on stdin and JSONL on stdout. every payload includes the stage
name, a session id, and a host-capability descriptor. results from
multiple scripts merge serially (arrays concat, scalars join with
newline).

```
host                                hook script
 |   spawn `<script> <stage>`        |
 | --------------------------------> |
 |   {"hook": "<stage>", ...} EOF    |
 | ---- stdin ---------------------> |
 |                                   |
 | <---- stdout (JSONL) ------------ |
 |   {"result": "..."}               |
 |   {"log": "debug line"}           |
 |                                   |
 | <---- exit 0 / non-zero --------- |
```

see [SPEC.md](SPEC.md) for the full wire contract.

## stages at a glance

stages are tiered by what the host needs to do to fire them. a script
that wants maximum portability stays in tier 0; scripts that need richer
instrumentation accept that not every host can fire higher tiers yet.

- **tier 0: universal.** every harness can fire these today.
  - `discover`, `mutate_request`, `before_tool`, `after_tool`,
    `execute_tool`
- **tier 1: loop-aware.** requires per-turn and end-of-loop visibility.
  - `before_turn`, `after_turn`, `before_stop`, `on_error`
- **tier 2: interception.** requires the harness to short-circuit a
  host-internal decision.
  - `on_permission`

per-host coverage and the conformance criteria live in
[CONFORMANCE.md](CONFORMANCE.md). planned work and upstream gaps live
in [ROADMAP.md](ROADMAP.md).

## what's deliberately not here

- **streaming chunk hooks** (`on_text_chunk`, `on_reasoning_chunk`).
  per-token volume makes shell hooks impractical; `after_turn` covers
  the same need at coarser granularity.
- **`after_run` as a separate stage.** folded into `before_stop`,
  which covers both observation (logging the final transcript) and
  intervention (forcing continuation).

hosts MAY define additional stages, payload fields, and response keys
on top of the protocol. the `host.stages` field of the base payload is
the discovery mechanism. host-specific extensions belong in that host's
own documentation, not here.

## reference implementations

- [hrns](https://github.com/khimaros/hrns): one-shot CLI host.
- [opencode-evolve](https://github.com/khimaros/opencode-evolve):
  opencode plugin host.
- [pi-evolve](https://github.com/khimaros/pi-evolve):
  pi-coding-agent extension host.

all three run identical hook scripts against the same contract.

## conformance testing

host integration tests capture the LLM request a harness produces when a hook
script is loaded by pointing the harness at [fake-openai], a standalone,
language-agnostic mock of an openai-compatible endpoint. it is a single binary
launched as a subprocess: it announces its url on stdout, serves a programmable
response stream, and records every request for inspection over an http admin
api.

the protocol-level assertions and the canonical `hello` fixture are shared,
living in [conformance/](conformance/) rather than being re-implemented in each
host. a host's integration test subclasses `HostAdapter` with the host-specific
seam (provider config, binary launch, env) and hands it to `run_conformance`;
the shared driver builds the sibling fake-openai binary's client, launches the
mock per scenario, reads `base_url=...` from its stdout, queries
`GET /__admin/captures`, and asserts protocol fidelity on the captured request.
host-unique scenarios (heartbeat, compaction, permission, ...) stay in each
host's test file and reuse the shared helpers. see
[conformance/README.md](conformance/README.md) and the fake-openai README.

[fake-openai]: https://github.com/khimaros/fake-openai
