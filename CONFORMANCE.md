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

| stage            | airun                              | pi-evolve                                   | opencode-evolve                                          |
| ---------------- | ---------------------------------- | ------------------------------------------- | -------------------------------------------------------- |
| `discover`       | local                              | local                                       | local                                                    |
| `mutate_request` | local                              | local                                       | local                                                    |
| `before_tool`    | local                              | local                                       | local (mutation response keys need upstream support)     |
| `after_tool`     | local                              | local                                       | local                                                    |
| `execute_tool`   | local                              | local                                       | local                                                    |
| `before_turn`    | -                                  | -                                           | needs upstream emission point                            |
| `after_turn`     | -                                  | -                                           | needs upstream emission point                            |
| `before_stop`    | local (observational; see limits)  | translates `turn_end` with no pending tools | translates opencode's `idle` event                       |
| `on_error`       | -                                  | -                                           | needs upstream emission point                            |
| `on_permission`  | -                                  | -                                           | needs upstream API                                       |

per-host gaps and tracking issues live in
[ROADMAP.md](ROADMAP.md) and in each project's own roadmap.
