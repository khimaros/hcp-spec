# hcp roadmap

planned work and known gaps in the protocol and its reference
implementations. items here are aspirational; consult each host's own
roadmap for scheduling.

## tier 1: loop-aware stages

- [ ] `before_turn` / `after_turn`: not implemented in any reference
  host yet. needs upstream emission points in opencode and pi; airun
  can implement locally.
- [ ] `on_error`: needs upstream emission point in opencode. airun
  and pi can implement locally.

## tier 2: interception

- [ ] `on_permission`: needs upstream API in opencode (permission
  engine is internal). airun and pi can implement locally.

## opencode-evolve upstream needs

- [ ] `before_tool.deny` short-circuit. opencode-evolve currently logs
  the deny intent but cannot actually short-circuit a tool call from
  `tool.execute.before`. needs upstream opencode plugin API.
- [ ] `mutate_request.tools` payload. opencode's plugin API does not
  expose the tool list at the relevant hook point; needs a different
  hook point or upstream work.

## protocol evolution

- [ ] formal version negotiation. `host.version` is currently a
  monotonic integer with no negotiation; future versions may break
  backward compatibility and need a richer capability handshake.

## conformance suite

- [x] unify conformance testing across the reference hosts. the shared
  driver and the canonical `hello` fixture live in `conformance/`; each
  host ships a thin `HostAdapter` instead of a standalone test that
  re-implements the protocol assertions.
- [x] assert the `host` capability block. the hello hook echoes the
  received `host` block into its system prompt and the driver verifies
  each host surfaces `host.{name, version, stages}` with version 2, the
  tier-0 universal stages, and canonical names only (no predecessor
  aliases). all three reference hosts pass.
