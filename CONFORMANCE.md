# hcp conformance

a host is conformant if:

1. it discovers and invokes hooks exactly as specified in
   [SPEC.md protocol invocation](SPEC.md#protocol-invocation).
2. it implements the stages listed as "local" in its column of the
   capability matrix below, including the observational semantics
   where applicable.
3. it advertises which stages it actually fires via the `host.stages`
   field of the base payload.

partial conformance is permitted but MUST be documented (e.g.
"observation-only: no `before_stop` continuation"). hooks SHOULD
degrade gracefully when stages are not invoked: the host capability
payload exposes which stages will fire.

## host capability matrix

"local" = implementable inside the harness without changes to its
upstream dependencies. "-" = not implemented. items marked "needs
upstream" require a PR to the upstream host.

| stage            | hrns                               | pi-evolve                                   | opencode-evolve                                          | hmux (`hmux face hcp`)                                   |
| ---------------- | ---------------------------------- | ------------------------------------------- | -------------------------------------------------------- | -------------------------------------------------------- |
| `discover`       | local                              | local                                       | local                                                    | local                                                    |
| `mutate_request` | local                              | local                                       | local                                                    | local                                                    |
| `before_tool`    | local                              | local                                       | local (mutation response keys need upstream support)     | local (deny/result short-circuit is a fidelity gap)      |
| `after_tool`     | local                              | local                                       | local                                                    | local                                                    |
| `execute_tool`   | local                              | local                                       | local                                                    | local (pi); opencode needs an in-process MCP shim        |
| `before_turn`    | -                                  | -                                           | needs upstream emission point                            | -                                                        |
| `after_turn`     | -                                  | -                                           | needs upstream emission point                            | -                                                        |
| `before_stop`    | local (observational; see limits)  | translates `turn_end` with no pending tools | translates opencode's `idle` event                       | -                                                        |
| `on_error`       | -                                  | -                                           | needs upstream emission point                            | -                                                        |
| `on_permission`  | -                                  | -                                           | needs upstream API                                       | local (hub-normalized across every backend)              |

hmux is a v3 host at the CLIENT layer (the `hmux face hcp` runner): it arms hmux's
normalized interception hooks, so the mapped stages work across every backend (pi,
opencode, ...) at once - notably `on_permission` is local for all of them. tool
registration (`discover.tools` / `execute_tool`) is generic on the pi backend; opencode
caches plugin tools, so it needs an in-process MCP shim. the v3 tier-3 extension stages
`heartbeat` (hub-scheduled; host-driven heartbeat conformance passes) and
`format_notification` are supported; `observe_message` and `recover` are not yet wired.
verified by the shared driver at `../hmux/e2e/hcp_conform_test.py` (56/56 against the
`hello` fixture, including the heartbeat battery; the host owns scheduling, so the driver
sets the face's `--heartbeat-every-secs` rather than the hook declaring a cadence).

per-host gaps and tracking issues live in
[ROADMAP.md](ROADMAP.md) and in each project's own roadmap.
