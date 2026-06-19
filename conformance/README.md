# hcp conformance suite

shared, host-agnostic conformance testing for hcp hosts. the protocol-level
assertions and the canonical `hello` fixture live here so the three reference
hosts ([airun], [pi-evolve], [opencode-evolve]) do not each re-implement them.

## how it works

a conformance run captures the llm request a host produces when the `hello`
hook is loaded, by pointing the host at [fake-openai] (a language-agnostic
openai-compatible mock that records every request over an http admin api), then
asserts protocol fidelity on the captured chat-completions request: hook tool
namespacing, parameter schemas, the priority enum round-trip, system-prompt
composition, and the `<env>` injection.

- `hcpconform.py` -- the shared driver: a `CheckRunner` pass/fail accumulator,
  openai-body extractors, capture finders, the `Fixture` loader, `seed_workspace`,
  and the protocol assertion helpers, tied together by `run_conformance`.
- `fixtures/hello/` -- the canonical hook (`hooks/hello.py`), its prompt
  contract (`prompts/*.md`), and `tests/hello_test.py`, which exercises the hook
  in isolation (no host, no mock).

the mock transport itself is not duplicated here: `hcpconform` imports the
`fakeopenai` python client from the sibling `../fake-openai` checkout, the same
way the host tests always have.

## wiring a host

a host repo's integration test stays thin: it subclasses `HostAdapter` with the
host-specific seam and hands it to `run_conformance`.

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "hcp-spec" / "conformance"))
import hcpconform as hc

class MyAdapter(hc.HostAdapter):
    name = "myhost"
    wants_heartbeat = True              # False for one-shot hosts
    builtin_tools = {"read", "bash"}    # the host's own tools

    def available(self):
        return (True, "") if my_binary_exists() else (False, "SKIP: build myhost")

    def build(self, runner):
        ...                             # compile plugin/binary; return False to abort

    def run_build(self, fixture):
        ws = hc.seed_workspace(tmp / "project", fixture)
        # write provider config pointing at the mock, launch the host, collect captures
        ...
        return hc.RunResult(build, heartbeat, captures, stdout, stderr)

    def extra_build_checks(self, body, fixture, runner):
        hc.assert_builtin_tools(body, self.builtin_tools, runner)
        hc.assert_param_descriptions(body, runner)
        hc.assert_system_preamble_chat(body, fixture, runner)  # if the host forwards prompts

runner = hc.CheckRunner()
adapter = MyAdapter()
if not hc.preflight(adapter):
    sys.exit(0)
hc.run_conformance(adapter, runner)
# ... host-unique scenarios add to the same runner ...
runner.summary()
sys.exit(runner.exit_code())
```

`run_build` owns the irreducibly host-specific work -- provider config, binary
launch, env, and the wait strategy. host-unique scenarios (compaction,
permission, skills, ...) stay in the host's own test file and reuse these
helpers against the same `CheckRunner`.

## the shared vs host-specific split

shared core (`assert_build_request`): request shape, the `<env>` block, the four
`hello_note_*` tools, the full `note_*` parameter-schema matrix, the priority-enum
json-schema round-trip, and the `host` capability block. `enum_values` normalizes
the two dialects hosts emit (`enum` vs `anyOf`/`oneOf` of `const`).

the host-capability check (`assert_host_capability`) covers CONFORMANCE.md item 3:
the hello hook echoes the `host` block it received into its system prompt as
`<hcp-host>{...}</hcp-host>`, and the driver reads it back out of the captured
request to verify the host advertises `name` + `version: 2` + a `stages` list that
includes the tier-0 universal stages and uses canonical names only (no predecessor
aliases like `tool_before`/`idle`/`turn_end`). this reuses the existing capture
rather than a separate hook-input probe.

host-specific (`extra_build_checks`): built-in tool names, prompt-file enums,
and whether the system prompt reproduces the hook's preamble + chat verbatim.

[airun]: https://github.com/khimaros/airun
[pi-evolve]: https://github.com/khimaros/pi-evolve
[opencode-evolve]: https://github.com/khimaros/opencode-evolve
[fake-openai]: https://github.com/khimaros/fake-openai
