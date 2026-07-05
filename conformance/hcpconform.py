"""shared hcp conformance harness.

the three reference hosts (airun, pi-evolve, opencode-evolve) each capture the
llm request their harness produces when the canonical `hello` hook is loaded,
point it at the shared fake-openai mock, and assert protocol fidelity on the
captured chat-completions request. this module holds the parts that were
copy-pasted into all three: the pass/fail accumulator, the openai-body
extractors, the capture finders, the fixture loader, workspace seeding, and the
protocol assertions themselves.

each host ships a thin HostAdapter (subclass below) that owns the irreducibly
host-specific seam -- writing its provider config pointed at the mock, launching
its binary, its env vars -- and returns a RunResult. the driver runs the shared
scenarios against it. host-unique scenarios (compaction, permission, skills, ...)
stay in each host's test file but reuse these helpers and one CheckRunner.

the mock transport is the shared fake-openai client, imported from the sibling
../fake-openai checkout the same way the host tests have always imported it.
"""

import json
import re
import shutil
import stat
import sys
import time
from pathlib import Path

# hcp-spec and the host repos are siblings; fake-openai is too. locate its
# python client relative to this file (conformance/ -> hcp-spec -> parent).
_SIBLINGS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SIBLINGS / "fake-openai" / "clients" / "python"))
import fakeopenai  # noqa: E402

# re-export the transport surface so host adapters import only this module.
available = fakeopenai.available
captures = fakeopenai.captures
chat_captures = fakeopenai.chat_captures
is_heartbeat_request = fakeopenai.is_heartbeat_request
program = fakeopenai.program
BIN = fakeopenai.BIN

# the canonical fixture lives beside this module; hosts seed a workspace from it.
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "hello"

# the prompt-contract files the hello hook's prompt tools enum-lock onto. recover
# has no file on disk (the hook handles it in code) but is part of the contract.
CONTRACT_PROMPTS = ["preamble.md", "chat.md", "heartbeat.md", "compaction.md", "recover.md"]

# note_write.priority is the canonical enum param; hosts emit it either as a
# json-schema `enum` or as an `anyOf`/`oneOf` of `const` (see enum_values).
NOTE_PRIORITIES = {"low", "normal", "high"}

# the host capability block (SPEC.md base payload) every host must surface to
# hooks. REQUIRED_STAGES is the tier-0 universal floor a host MUST advertise;
# it MAY advertise more (before_stop, host-specific extensions). PREDECESSOR_STAGES
# are internal event names hosts translate FROM -- they must not leak into the
# advertised, canonical-only host.stages.
REQUIRED_STAGES = {"discover", "mutate_request", "before_tool", "after_tool", "execute_tool"}
PREDECESSOR_STAGES = {"tool_before", "tool_after", "idle", "turn_end", "run_end", "after_run"}
HOST_BLOCK_RE = re.compile(r"<hcp-host>(.*?)</hcp-host>", re.DOTALL)
CWD_BLOCK_RE = re.compile(r"<hcp-cwd>(.*?)</hcp-cwd>", re.DOTALL)

try:
    import jsonschema
except ImportError:
    jsonschema = None


def start_fake_openai(*args):
    """launch the mock on a free port and block until its url is announced."""
    return fakeopenai.FakeOpenAI(*args).start()


class CheckRunner:
    """pass/fail accumulator shared by the driver and each host's local extras.
    one runner per test process so the final tally spans every scenario."""

    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, desc, ok, detail=""):
        if ok:
            self.passed += 1
            print(f"PASS: {desc}")
        else:
            self.failed += 1
            print(f"FAIL: {desc}")
            if detail:
                print(f"  {detail}")

    def summary(self):
        print(f"\n{self.passed} passed, {self.failed} failed")

    def exit_code(self):
        return 1 if self.failed else 0


# --- openai chat-completions body extractors ---

def _content_text(content):
    """flatten an openai message `content` (string, or list of text parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def role_text(body, *roles):
    """joined text of every message whose role is in `roles`."""
    return "\n".join(_content_text(m.get("content"))
                     for m in body.get("messages", []) if m.get("role") in roles)


def system_text(body):
    return role_text(body, "system", "developer")


def user_text(body):
    return role_text(body, "user")


def tool_function(body, name):
    """the `function` object of a tool by name, or {}."""
    for t in body.get("tools") or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if fn and fn.get("name") == name:
            return fn
    return {}


def tool_names(body):
    return {(t.get("function") or {}).get("name")
            for t in body.get("tools") or [] if isinstance(t, dict)} - {None}


def tool_params(body, name):
    return tool_function(body, name).get("parameters") or {}


def prop(body, name, field):
    return (tool_params(body, name).get("properties") or {}).get(field) or {}


def required(body, name):
    return set(tool_params(body, name).get("required") or [])


def enum_values(schema):
    """allowed values of an enum-shaped param, normalizing the two dialects
    hosts emit: a json-schema `enum`, or an `anyOf`/`oneOf` of `const`."""
    if "enum" in schema:
        return set(schema["enum"])
    variants = schema.get("anyOf") or schema.get("oneOf") or []
    return {v.get("const") for v in variants if isinstance(v, dict) and "const" in v}


# --- capture finders ---

def find_build_request(caps):
    """the build request: a tools-bearing chat call that is not a heartbeat."""
    return next((c for c in caps
                 if "chat/completions" in c["path"]
                 and c["body"].get("tools")
                 and not is_heartbeat_request(c["body"])), None)


def find_heartbeat_request(caps):
    return next((c for c in caps
                 if "chat/completions" in c["path"]
                 and is_heartbeat_request(c["body"])), None)


def poll_for(predicate, proc, deadline_s, interval=0.2):
    """poll `predicate()` until truthy, the deadline passes, or `proc` exits
    (after which we sample once more so a fast one-shot run is not missed)."""
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        if proc is not None and proc.poll() is not None:
            time.sleep(0.3)
            return predicate()
        time.sleep(interval)
    return predicate()


# --- fixture ---

class Fixture:
    """the canonical hello hook plus its prompt contract. exposes prompt bodies
    so assertions can demand they reach the llm verbatim."""

    def __init__(self, root=FIXTURE_DIR):
        self.root = Path(root)
        self.prompts = self.root / "prompts"
        self.hook = self.root / "hooks" / "hello.py"

    def exists(self):
        return self.hook.exists()

    def prompt(self, name):
        return (self.prompts / name).read_text().strip()

    @property
    def preamble(self):
        return self.prompt("preamble.md")

    @property
    def chat(self):
        return self.prompt("chat.md")

    @property
    def heartbeat(self):
        return self.prompt("heartbeat.md")

    @property
    def compaction(self):
        return self.prompt("compaction.md")


def dump_artifacts(art_dir, prefix, result):
    """write the captured build request and host output for human inspection.
    purely a debugging aid; nothing reads these back as expectations."""
    art_dir = Path(art_dir)
    art_dir.mkdir(parents=True, exist_ok=True)
    if result.build is not None:
        (art_dir / f"{prefix}.build_request.json").write_text(
            json.dumps(result.build, indent=2, default=str))
    (art_dir / f"{prefix}.stdout.log").write_text(result.stdout or "")
    (art_dir / f"{prefix}.stderr.log").write_text(result.stderr or "")


def seed_workspace(dest, fixture=None, *, make_git=False):
    """copy the fixture tree into `dest` and mark its hooks executable, matching
    how a host discovers a workspace. `make_git` adds a .git marker for hosts
    (airun) whose discovery walks up to a repo root."""
    fixture = fixture or Fixture()
    shutil.copytree(fixture.root, dest)
    for p in (dest / "hooks").iterdir():
        if p.is_file():
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if make_git:
        (dest / ".git").mkdir(exist_ok=True)
    return dest


# --- shared protocol assertions ---

def assert_request_shape(body, runner):
    """the captured request is a well-formed chat-completions call."""
    runner.check("request body has 'model'", "model" in body, f"keys: {list(body.keys())}")
    runner.check("request body has 'messages'", isinstance(body.get("messages"), list))
    runner.check("request body has 'tools'", isinstance(body.get("tools"), list))


def assert_env_block(body, runner):
    """hello's mutate_request injection (the <env> block) reached the llm."""
    text = system_text(body)
    runner.check("has system message(s)", len(text) > 0)
    runner.check("system prompt non-empty", len(text) > 50, f"len={len(text)}")
    runner.check("system prompt includes hello <env> block",
                 "<env>" in text and "</env>" in text, text[:400])
    runner.check("system prompt includes 'Session start time:' from <env> block",
                 "Session start time:" in text)


def assert_hook_tools(body, runner):
    """the hello hook's four note_* tools are registered and namespaced."""
    names = tool_names(body)
    runner.check("tools array non-empty", len(body.get("tools") or []) > 0)
    expected = {"hello_note_list", "hello_note_read", "hello_note_write", "hello_note_delete"}
    runner.check("all hello hook tools registered", expected <= names,
                 f"missing: {sorted(expected - names)}; got: {sorted(names)}")


def assert_builtin_tools(body, builtin_tools, runner):
    """the host's own built-in tools surface alongside the hook's tools."""
    names = tool_names(body)
    for builtin in sorted(builtin_tools):
        runner.check(f"built-in tool registered: {builtin}", builtin in names)


def assert_every_tool_has_description(body, runner):
    """every registered tool carries a non-empty description."""
    missing = sorted(n for n in tool_names(body)
                     if not tool_function(body, n).get("description"))
    runner.check("every tool has a description", not missing, f"missing description: {missing}")


def assert_param_descriptions(body, runner, prefixes=None):
    """every parameter of the in-scope tools carries a description. enum-shaped
    params (anyOf of consts) may describe the wrapper instead of the value, so
    they are exempt. `prefixes` scopes which tools count (None = all)."""
    missing = []
    for t in body.get("tools") or []:
        fn = t.get("function") or {}
        name = fn.get("name") or ""
        if prefixes and not name.startswith(tuple(prefixes)):
            continue
        for pname, pschema in ((fn.get("parameters") or {}).get("properties") or {}).items():
            if pschema.get("description") or pschema.get("anyOf"):
                continue
            missing.append(f"{name}.{pname}")
    runner.check("every tool parameter has a description", not missing,
                 f"missing: {missing[:10]}")


def assert_note_schemas(body, runner):
    """note_* parameter schemas preserve type, required-vs-optional, and the
    priority enum end-to-end through the host's tool serialization."""
    p = prop(body, "hello_note_list", "include_hidden")
    runner.check("note_list.include_hidden is boolean", p.get("type") == "boolean", f"got: {p}")
    runner.check("note_list.include_hidden is optional",
                 "include_hidden" not in required(body, "hello_note_list"))

    p = prop(body, "hello_note_read", "name")
    runner.check("note_read.name is string", p.get("type") == "string", f"got: {p}")
    runner.check("note_read.name is required", "name" in required(body, "hello_note_read"))
    p = prop(body, "hello_note_read", "limit")
    runner.check("note_read.limit is number", p.get("type") == "number", f"got: {p}")
    runner.check("note_read.limit is optional", "limit" not in required(body, "hello_note_read"))

    for field in ("name", "content"):
        p = prop(body, "hello_note_write", field)
        runner.check(f"note_write.{field} is string", p.get("type") == "string", f"got: {p}")
        runner.check(f"note_write.{field} is required",
                     field in required(body, "hello_note_write"))
    runner.check("note_write.tags is optional", "tags" not in required(body, "hello_note_write"))
    p = prop(body, "hello_note_write", "metadata")
    runner.check("note_write.metadata is object", p.get("type") == "object", f"got: {p}")
    runner.check("note_write.metadata is optional",
                 "metadata" not in required(body, "hello_note_write"))
    p = prop(body, "hello_note_write", "priority")
    runner.check("note_write.priority enum is [low, normal, high]",
                 enum_values(p) == NOTE_PRIORITIES, f"got: {p}")
    runner.check("note_write.priority is optional",
                 "priority" not in required(body, "hello_note_write"))

    p = prop(body, "hello_note_delete", "name")
    runner.check("note_delete.name is string", p.get("type") == "string", f"got: {p}")
    runner.check("note_delete.name is required", "name" in required(body, "hello_note_delete"))


def assert_note_tags_array(body, runner):
    """note_write.tags is an element-typed array (string items). this is an
    extended tool-parameter form (SPEC.md): hosts MAY support it, so it is opt-in
    rather than part of the universal note-schema core."""
    p = prop(body, "hello_note_write", "tags")
    runner.check("note_write.tags is array", p.get("type") == "array", f"got: {p}")
    runner.check("note_write.tags items are string",
                 (p.get("items") or {}).get("type") == "string", f"got items: {p.get('items')}")


def assert_enum_roundtrip(body, runner):
    """the captured note_write schema is valid json-schema and its priority enum
    is a real constraint: a valid value passes and an out-of-set value is
    rejected. proves the enum survived as more than metadata."""
    if jsonschema is None:
        runner.check("jsonschema available for enum round-trip", False,
                     "skipped; install jsonschema to enable")
        return
    schema = tool_params(body, "hello_note_write")
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        runner.check("note_write parameters is a valid JSON Schema", True)
    except jsonschema.exceptions.SchemaError as e:
        runner.check("note_write parameters is a valid JSON Schema", False,
                     str(e).splitlines()[0])
        return
    valid_ok = True
    try:
        jsonschema.validate({"name": "todo.md", "content": "x", "priority": "high"}, schema)
    except jsonschema.ValidationError as e:
        valid_ok = False
        valid_err = str(e).splitlines()[0]
    runner.check("note_write valid enum value passes jsonschema validation",
                 valid_ok, "" if valid_ok else valid_err)
    rejected = False
    try:
        jsonschema.validate({"name": "todo.md", "content": "x", "priority": "urgent"}, schema)
    except jsonschema.ValidationError:
        rejected = True
    runner.check("note_write invalid enum value is rejected by jsonschema",
                 rejected, "expected ValidationError, got none")


def assert_system_preamble_chat(body, fixture, runner):
    """hosts that forward the prompt contract compose the build system prompt
    from the hook's preamble + chat stage; heartbeat must not leak in."""
    text = system_text(body)
    runner.check("system prompt contains hello preamble verbatim",
                 fixture.preamble in text, text[:500])
    runner.check("system prompt contains hello chat stage verbatim",
                 fixture.chat in text, text[:500])
    runner.check("system prompt does NOT include heartbeat stage body",
                 fixture.heartbeat not in text, text[:500])


def assert_host_capability(body, adapter, runner):
    """the host advertises its capability block ({name, version, stages}) in the
    base payload it sends to hooks (SPEC.md base payload; CONFORMANCE.md item 3).
    the hello hook echoes the block it received into its system prompt, so we read
    it back out of the captured build request."""
    m = HOST_BLOCK_RE.search(system_text(body))
    runner.check("host capability block present in hook payload", m is not None)
    if not m:
        return
    try:
        host = json.loads(m.group(1))
    except ValueError as e:
        runner.check("host capability block is valid JSON", False, str(e))
        return
    runner.check(f"host.name advertised as '{adapter.name}'",
                 host.get("name") == adapter.name, f"got: {host.get('name')}")
    runner.check(f"host.version is {adapter.version}",
                 host.get("version") == adapter.version, f"got: {host.get('version')}")
    stages = set(host.get("stages") or [])
    runner.check("host.stages advertises the tier-0 universal stages",
                 REQUIRED_STAGES <= stages, f"missing: {sorted(REQUIRED_STAGES - stages)}")
    runner.check("host.stages uses canonical names (no predecessor aliases)",
                 not (PREDECESSOR_STAGES & stages), f"leaked: {sorted(PREDECESSOR_STAGES & stages)}")


def assert_cwd(body, runner):
    """v3: the base payload carries `cwd` (the workspace root), so a hook reads its own
    files - prompts, data, state. the hello hook echoes it into the system prompt; read it
    back out of the captured request."""
    m = CWD_BLOCK_RE.search(system_text(body))
    runner.check("base payload carries cwd (v3)",
                 m is not None and bool(m.group(1).strip()),
                 f"got: {m.group(1) if m else None}")


def assert_build_request(body, fixture, adapter, runner):
    """the protocol core every conformant host shares for a build request."""
    assert_request_shape(body, runner)
    assert_env_block(body, runner)
    assert_hook_tools(body, runner)
    assert_note_schemas(body, runner)
    assert_enum_roundtrip(body, runner)
    assert_host_capability(body, adapter, runner)
    if adapter.version >= 3:
        assert_cwd(body, runner)
    adapter.extra_build_checks(body, fixture, runner)


def assert_heartbeat_request(body, fixture, runner):
    """a heartbeat fires in a fresh session: its user turn carries the
    [heartbeat] sentinel + hello's heartbeat body, its system prompt has the
    preamble + heartbeat stage but not the chat stage, and it carries tools."""
    if body is None:
        runner.check("heartbeat chat/completions request captured", False)
        return
    runner.check("heartbeat chat/completions request captured", True)
    u = user_text(body)
    s = system_text(body)
    runner.check("heartbeat: user message has [heartbeat] prefix", "[heartbeat]" in u, u[:300])
    runner.check("heartbeat: user message contains hello heartbeat body",
                 fixture.heartbeat in u, u[:500])
    runner.check("heartbeat: system prompt non-empty", len(s) > 50, f"len={len(s)}")
    runner.check("heartbeat: system prompt contains hello preamble verbatim",
                 fixture.preamble in s, s[:500])
    runner.check("heartbeat: system prompt contains heartbeat stage verbatim",
                 fixture.heartbeat in s, s[:500])
    runner.check("heartbeat: system prompt does NOT include chat stage body",
                 fixture.chat not in s, s[:500])
    runner.check("heartbeat: system prompt contains <env> block",
                 "<env>" in s and "Session start time:" in s, s[:500])
    runner.check("heartbeat: fresh session -- only one user message in history",
                 len([m for m in body.get("messages", []) if m.get("role") == "user"]) == 1)
    runner.check("heartbeat: request carries tools", len(body.get("tools") or []) > 0)


# --- adapter contract + driver ---

class RunResult:
    """what a host's run_build returns: the build request body (or None), the
    optional heartbeat request, the raw captures, and the host's output."""

    def __init__(self, build, heartbeat, caps, stdout, stderr):
        self.build = build
        self.heartbeat = heartbeat
        self.captures = caps
        self.stdout = stdout
        self.stderr = stderr


class HostAdapter:
    """the host-specific seam. subclasses own provider config, launch, and env;
    everything downstream of `run_build` is shared. see the three host repos for
    concrete subclasses."""

    name = "?"
    # the HCP host protocol version the host advertises (host.version). v3 is the default
    # baseline (base-payload cwd, hook-owned prompts); a still-v2 host sets `version = 2`.
    version = 3
    wants_heartbeat = False
    builtin_tools = set()

    def available(self):
        """(ok, message). when not ok, the message is printed and the run skips
        (or fails) without asserting. fake-openai + fixture are checked already."""
        return True, ""

    def build(self, runner):
        """compile the host (plugin/binary) if needed. return False to abort."""
        return True

    def run_build(self, fixture):
        """seed a workspace, launch the host against the mock, and return a
        RunResult. owns the wait strategy (one-shot vs stalled-with-heartbeat)."""
        raise NotImplementedError

    def extra_build_checks(self, body, fixture, runner):
        """host-specific build-request assertions (built-in tools, prompt enums,
        system-prompt fidelity, ...). composed from the shared helpers above."""


def preflight(adapter, fixture=None):
    """shared skip gates: the mock binary, the fixture, then the host's own.
    prints a SKIP/FAIL line and returns False when the run cannot proceed."""
    fixture = fixture or Fixture()
    if not available():
        print(f"SKIP: fake-openai binary not found at {BIN}; "
              "build ../fake-openai or set FAKE_OPENAI_BIN")
        return False
    if not fixture.exists():
        print(f"SKIP: hcp conformance fixture not found at {fixture.root}; "
              "check out ../hcp-spec")
        return False
    ok, message = adapter.available()
    if not ok:
        print(message)
        return False
    return True


def run_conformance(adapter, runner, fixture=None):
    """run the shared build (+ heartbeat) scenario for `adapter`, recording into
    `runner`. returns the RunResult so the caller can add host-local scenarios."""
    fixture = fixture or Fixture()
    result = adapter.run_build(fixture)
    runner.check("build chat/completions request captured", result.build is not None,
                 f"captured paths: {[c['path'] for c in result.captures]}\n"
                 f"stderr tail:\n{(result.stderr or '')[-1000:]}")
    if result.build is not None:
        assert_build_request(result.build["body"], fixture, adapter, runner)
    if adapter.wants_heartbeat:
        hb = result.heartbeat["body"] if result.heartbeat else None
        assert_heartbeat_request(hb, fixture, runner)
    return result
