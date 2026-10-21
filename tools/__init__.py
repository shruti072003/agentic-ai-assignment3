"""RepoMind's tools, and the registry that runs them.

Each tool is two files with one name: tools/<module>.json, its definition, the only
thing the model sees of it; and tools/<module>.py, the code that runs it. Rename a
tool inside its .json file and keep both file names. tools/roles.yaml says which
tool plays each of RepoMind's five roles (checks/FORMATS.md, section 11.2).

The registry's interface, which the runner, your ways and the checker use
(checks/FORMATS.md, section 11.3):

    registry = ToolRegistry(ctx)          # ctx is a RunContext, below
    registry.schemas                      # what the vendor gets: name, description, input_schema, strict
    registry.names()                      # the tools' names, in MODULES order
    registry.definition(name)             # the whole definition file, examples included
    registry.hooks.append(hook)           # hook(name, tool_input) runs before every call
    registry.call(name, tool_input, plan_step=None) -> ToolResult
    registry.check_args(name, tool_input) -> str | None   # the boundary alone, nothing runs

Every call goes through the same gates, in this order (Assignment 3):

  1. Protocol (Part B, task 2). An unknown tool, or arguments that are not a JSON
     object, is for the harness, not the model: the full error goes to
     runs/<run_id>.harness.log, and the model gets GENERIC, which names no tool.
  2. The boundary (Part A, task 6). The arguments against the input schema, with
     enum values compared case-insensitively, then the tool's content rules
     (lengths, ranges, globs), which strict mode cannot hold: each module's
     validate(). A rejected call never runs; its error names every bad field and
     what would be right.
  3. Hooks (Part C, task 5). Each runs before the tool; a hook that returns
     anything but None, or raises, denies the call (fail closed).
  4. The tool. A read that fails transiently is retried here, below the model,
     at most READ_TRIES times. A write is keyed and goes through write() below.
  5. The result against the shape its definition declares (result_schema),
     before the model sees it.

Every attempt leaves one line in the call record (ctx.calls), whatever the agent says.

Writes (Part C, tasks 2 to 4, 6). The key is the harness's, never the model's:
sha256 of the run's id, the plan step's id and the arguments in canonical form
(derive_key). Before every attempt, a check: an open PR from the issue's branch in
this run, or a comment with this attempt's key; one found is the result. A timeout
is ambiguous, so it is read back: found, the write happened, and the run goes on;
not found, it is retried with the same key and the same arguments; unreadable, the
registry cannot tell, and the result carries escalate=True so the way ends the run
escalated. At most WRITE_ATTEMPTS attempts; still unconfirmed after the last, the
same. KEY_MODE=uuid sends a fresh random key on every attempt, and VERIFY=off skips
the read-back and retries blind: Part D's control.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sqlite3
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent

# The five modules, in the order the registry offers them. Keep these file names.
MODULES = ("read_issue", "search_repo", "read_file", "post_comment", "open_pr")

# The fields of a definition the vendor gets. Everything else in the file (the
# examples, the result's shape) stays here; the examples go into the description.
SENT = ("name", "description", "input_schema", "strict")

READ_TRIES = 3          # a transient read failure is retried here, below the model
WRITE_ATTEMPTS = 3      # a write still unconfirmed after this many attempts escalates
ERROR_CHARS = 900       # an error the model sees is capped; its repair fact comes first
TRANSIENT = (sqlite3.OperationalError, BlockingIOError, InterruptedError)

# What the model gets for a protocol error: no tool's name, no detail (at most 200 characters).
GENERIC = ("Error: that call could not be run, and no change to its arguments will fix it. "
           "Use only the tools you were offered, with a JSON object of arguments.")
INTERNAL = ("Error: the tool failed inside the harness; this is not caused by your arguments and retrying "
            "will not help. Continue without this result.")
ESCALATED = ("This write could not be confirmed: it may or may not have happened, and the tracker cannot be read "
             "back. The harness has stopped the run and asked a person. Do not retry it.")


class ToolError(Exception):
    """A tool failed in a way the model can be told about. Becomes an is_error result."""


@dataclass
class ToolResult:
    content: str
    is_error: bool
    latency_s: float
    outcome: str = "ok"       # the call record's outcome for the attempt the model is told about
    escalate: bool = False    # a write that could not be confirmed: the way ends the run escalated


@dataclass
class RunContext:
    run_id: str
    tracker: object                    # harness.tracker.Tracker
    repo_root: object                  # pathlib.Path to fixtures/repo
    calls: object = None               # harness.calls.CallRecord: runs/<run_id>.calls.jsonl
    harness_log: object = None         # pathlib.Path: runs/<run_id>.harness.log, for protocol errors
    switches: dict = field(default_factory=dict)   # harness.switches.current(), as the runner read them


def load_definition(module: str) -> dict:
    return json.loads((TOOLS_DIR / f"{module}.json").read_text())


def canonical(args: dict) -> str:
    """Arguments in canonical form: keys sorted, no whitespace."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def derive_key(run_id: str, plan_step, args: dict) -> str:
    """A write's idempotency key: the same intended action gets the same key on every retry."""
    material = f"{run_id}|{json.dumps(plan_step)}|{canonical(args)}"
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def rendered(definition: dict) -> str:
    """The description with the examples at its end, which every vendor reads."""
    lines = [definition.get("description", "")]
    examples = definition.get("examples") or []
    if examples:
        lines.append("\nExamples:")
        for i, example in enumerate(examples, 1):
            kind = "typical" if i == 1 else "hard"
            lines.append(f"{i}. ({kind}) {canonical(example.get('input') or {})} - {example.get('note', '')}")
    return "\n".join(lines)


SECRET = re.compile(r"\b[A-Z][A-Z0-9_]*(KEY|SECRET|TOKEN|PASS|PASSWORD)\s*=\s*\S+")


def no_secrets(name: str, tool_input: dict) -> str | None:
    """RepoMind's own hook: a public write never carries a secret-looking assignment
    (config/secrets.env's shape). Every other call is allowed."""
    from harness.roles import WRITES, role_of
    if role_of(name) in WRITES and any(SECRET.search(str(v)) for v in tool_input.values()):
        return "a public write may not contain a credential assignment (NAME_KEY=value); remove it and call again"
    return None


class ToolRegistry:
    def __init__(self, ctx: RunContext, modules=MODULES):
        self.ctx = ctx
        self.hooks: list = [no_secrets]
        self._tools: dict[str, tuple[dict, object]] = {}
        for module in modules:
            definition = load_definition(module)
            self._tools[definition["name"]] = (definition, importlib.import_module(f"tools.{module}"))

    @property
    def schemas(self) -> list[dict]:
        """The definitions as the vendor gets them, in MODULES order, ready for
        ModelClient.create(tools=...): the four SENT fields, examples in the description."""
        out = []
        for d, _ in self._tools.values():
            sent = {k: d[k] for k in SENT if k in d}
            sent["description"] = rendered(d)
            out.append(sent)
        return out

    def names(self) -> list[str]:
        return list(self._tools)

    def definition(self, name: str) -> dict:
        return self._tools[name][0]

    # -- the call -------------------------------------------------------------------------
    def call(self, name: str, tool_input: dict, plan_step=None) -> ToolResult:
        """Run one tool through the five gates. Expected failures come back as is_error
        results; nothing a model sends can make this raise.

        `plan_step` is the id of the plan step the call runs for, from ctx.call_tool,
        or None when the model chose the call (the agent's loop)."""
        start = time.monotonic()
        why = self._protocol_problem(name, tool_input)
        if why:
            return self._protocol(name, tool_input, why, plan_step, start)
        definition, module = self._tools[name]
        args, problems = self._boundary(definition, module, tool_input)
        if problems:
            text = self._cap(f"Invalid arguments for {name}; nothing ran. " + "; ".join(problems)
                             + f". A valid call: {canonical((definition.get('examples') or [{}])[0].get('input') or {})}")
            self._record(name, tool_input, "rejected", True, text, start, plan_step)
            return ToolResult(text, True, time.monotonic() - start, "rejected")
        denial = self._hooks(name, args)
        if denial is not None:
            text = self._cap(f"Denied before it ran: {denial}. Nothing was done.")
            self._record(name, args, "denied", True, text, start, plan_step)
            return ToolResult(text, True, time.monotonic() - start, "denied")
        from harness.roles import WRITES, role_of
        role = role_of(name)
        if role in WRITES:
            return self._write(name, role, definition, module, args, plan_step)
        return self._read(name, definition, module, args, plan_step)

    def check_args(self, name: str, tool_input: dict) -> str | None:
        """The boundary alone, for a draft the executor will send later: the error the
        call would get, or None. Nothing runs and nothing is recorded."""
        why = self._protocol_problem(name, tool_input)
        if why:
            return GENERIC
        definition, module = self._tools[name]
        _, problems = self._boundary(definition, module, tool_input)
        return self._cap(f"Invalid arguments for {name}: " + "; ".join(problems)) if problems else None

    # -- 1. protocol --------------------------------------------------------------------------
    def _protocol_problem(self, name, tool_input) -> str | None:
        if name not in self._tools:
            return f"unknown tool {name!r}; this registry has {self.names()}"
        if not isinstance(tool_input, dict):
            return f"arguments are a {type(tool_input).__name__}, not a JSON object: {tool_input!r:.300}"
        if "_unparsed_arguments" in tool_input:
            return f"arguments are not valid JSON: {tool_input['_unparsed_arguments']!r:.300}"
        return None

    def _protocol(self, name, tool_input, why: str, plan_step, start) -> ToolResult:
        self._log(f"protocol error: {why}; call {name!r} with {tool_input!r:.1000} (plan step {plan_step})")
        self._record(name, tool_input if isinstance(tool_input, dict) else {"value": tool_input}, "protocol",
                     True, GENERIC, start, plan_step)
        return ToolResult(GENERIC, True, time.monotonic() - start, "protocol")

    def _log(self, text: str) -> None:
        if self.ctx.harness_log is None:
            return
        path = Path(self.ctx.harness_log)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {self.ctx.run_id} {text}\n")

    # -- 2. the boundary ----------------------------------------------------------------------
    def _boundary(self, definition: dict, module, tool_input: dict) -> tuple[dict, list[str]]:
        """(the arguments, enum values in their canonical case; every problem, each naming its field)."""
        from harness.schema import errors
        schema = definition.get("input_schema") or {}
        props = schema.get("properties") or {}
        args = dict(tool_input)
        for key, spec in props.items():
            value = args.get(key)
            if isinstance(value, str) and spec.get("enum"):
                match = next((e for e in spec["enum"] if isinstance(e, str) and e.lower() == value.strip().lower()), None)
                args[key] = match if match is not None else value
        problems = []
        for line in errors(args, schema):
            field_name = line.split(":", 1)[0].split(".")[0].split("[")[0]
            spec = props.get(field_name)
            if "not in the schema" in line:
                problems.append(f"{field_name}: not an argument of this tool; its arguments are {', '.join(props)}")
            elif "required, and missing" in line:
                nullable = isinstance(spec, dict) and "null" in (spec.get("type") or [])
                problems.append(f"{field_name}: required and missing" + ("; pass null to leave it unset" if nullable else ""))
            elif "is not one of" in line:
                problems.append(f"{field_name}: {args.get(field_name)!r} is not allowed; use one of "
                                f"{', '.join(map(str, spec.get('enum') or []))}")
            elif "where the schema wants" in line:
                wanted = line.rsplit("where the schema wants", 1)[1].strip()
                problems.append(f"{field_name}: got {type(args.get(field_name)).__name__} "
                                f"{json.dumps(args.get(field_name), default=str)[:60]}, want {wanted}")
            else:
                problems.append(line)
        if not problems and hasattr(module, "validate"):
            problems = module.validate(self.ctx, args)
        return args, problems

    def _cap(self, text: str) -> str:
        return text if len(text) <= ERROR_CHARS else text[:ERROR_CHARS - 12] + " [truncated]"

    # -- 3. hooks -------------------------------------------------------------------------------
    def _hooks(self, name: str, args: dict) -> str | None:
        """The first denial, or None. A hook that raises denies: fail closed."""
        for hook in list(self.hooks):
            try:
                verdict = hook(name, dict(args))
            except Exception as exc:
                self._log(f"hook {getattr(hook, '__name__', hook)!r} raised on {name}: {type(exc).__name__}: {exc}")
                return f"a policy check failed ({type(exc).__name__}), so the call is denied"
            if verdict is not None:
                return str(verdict) if not isinstance(verdict, bool) else "a policy check denied it"
        return None

    # -- 4 and 5. reads -------------------------------------------------------------------------
    def _read(self, name, definition, module, args, plan_step) -> ToolResult:
        for attempt in range(1, READ_TRIES + 1):
            start = time.monotonic()
            try:
                content = module.run(self.ctx, **args)
            except ToolError as exc:
                text = self._cap(f"Error: {exc}")
                self._record(name, args, "error", True, text, start, plan_step, attempt=attempt)
                return ToolResult(text, True, time.monotonic() - start, "error")
            except TRANSIENT as exc:
                text = f"transient {type(exc).__name__}: {exc}"
                self._record(name, args, "error", True, text, start, plan_step, attempt=attempt)
                if attempt < READ_TRIES:
                    time.sleep(0.05 * 2 ** attempt)
                    continue
                text = (f"Error: {name} is unavailable for a moment ({type(exc).__name__}). Retry after 30 seconds "
                        "with the same arguments; they are not the problem.")
                return ToolResult(text, True, time.monotonic() - start, "error")
            except Exception as exc:
                return self._internal(name, args, exc, start, plan_step, attempt)
            return self._checked(name, definition, args, content, start, plan_step, attempt)
        raise AssertionError("unreachable")

    def _checked(self, name, definition, args, content, start, plan_step, attempt, key=None) -> ToolResult:
        """The result against its declared shape, before the model sees it."""
        problem = self._shape_problem(definition, content)
        if problem:
            self._log(f"result of {name} broke its declared shape: {problem}; content {content[:500]!r}")
            self._record(name, args, "error", True, INTERNAL, start, plan_step, key=key, attempt=attempt)
            return ToolResult(INTERNAL, True, time.monotonic() - start, "error")
        self._record(name, args, "ok", False, content, start, plan_step, key=key, attempt=attempt)
        return ToolResult(content, False, time.monotonic() - start, "ok")

    def _shape_problem(self, definition, content) -> str | None:
        shape = definition.get("result_schema")
        if not shape:
            return None
        from harness.schema import errors
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return "not JSON"
        broken = errors(data, shape)
        return "; ".join(broken[:3]) if broken else None

    def _internal(self, name, args, exc, start, plan_step, attempt, key=None) -> ToolResult:
        self._log(f"tool {name} raised {type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}")
        self._record(name, args, "error", True, INTERNAL, start, plan_step, key=key, attempt=attempt)
        return ToolResult(INTERNAL, True, time.monotonic() - start, "error")

    # -- 4. writes: the key, the check before, the three exits ------------------------------------
    def _write(self, name, role, definition, module, args, plan_step) -> ToolResult:
        from harness.tracker import ReadbackUnavailable, TrackerTimeout
        switches = self.ctx.switches or {}
        fresh_keys = switches.get("key_mode") == "uuid"         # the control: a new key on every attempt
        verify = switches.get("verify", "on") != "off"          # the control: retry blind
        derived = derive_key(self.ctx.run_id, plan_step, args)
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            key = uuid.uuid4().hex if fresh_keys else derived
            start = time.monotonic()
            try:                                                # check before you execute
                existing = self._existing(role, args, key)
            except ReadbackUnavailable as exc:
                return self._escalate(name, f"the check before attempt {attempt} could not read the tracker: {exc}")
            if existing is not None:
                text = module.result(existing, replayed=True)
                self._record(name, args, "ok", False, f"already in the tracker: {text}", start, plan_step,
                             key=existing.get("key"), attempt=attempt)
                return ToolResult(text, False, time.monotonic() - start, "ok")
            try:
                content = module.run(self.ctx, **args, idempotency_key=key)
            except TrackerTimeout as exc:
                self._record(name, args, "timeout", True, f"timeout: {exc}", start, plan_step, key=key, attempt=attempt)
                if not verify:
                    continue                                    # the control: retry blind
                try:                                            # did it happen? read it back
                    found = self._readback(role, args, key)
                except ReadbackUnavailable as exc2:             # cannot tell: a person decides
                    return self._escalate(name, f"attempt {attempt} timed out and could not be read back: {exc2}")
                if found is not None:                           # yes: it happened; go on with it
                    text = module.result(found, replayed=False)
                    self._record(name, args, "ok", False, f"read back after the timeout: {text}", start, plan_step,
                                 key=key, attempt=attempt)
                    return ToolResult(text, False, time.monotonic() - start, "ok")
                continue                                        # no: retry, same key, same arguments
            except ToolError as exc:
                text = self._cap(f"Error: {exc}")
                self._record(name, args, "error", True, text, start, plan_step, key=key, attempt=attempt)
                return ToolResult(text, True, time.monotonic() - start, "error")
            except Exception as exc:
                return self._internal(name, args, exc, start, plan_step, attempt, key=key)
            return self._checked(name, definition, args, content, start, plan_step, attempt, key=key)
        return self._escalate(name, f"still unconfirmed after {WRITE_ATTEMPTS} attempts")

    def _existing(self, role: str, args: dict, key: str) -> dict | None:
        """The check before an attempt: this run's open PR from the issue's branch, or its comment with this key."""
        from harness.roles import arg
        tracker = self.ctx.tracker
        if role == "open_pr":
            found = tracker.prs(self.ctx.run_id, branch=args[arg("open_pr", "branch")])
            return found[0] if found else None
        return tracker.comment_by_key(self.ctx.run_id, key)

    def _readback(self, role: str, args: dict, key: str) -> dict | None:
        """After a timeout: did this write happen?"""
        tracker = self.ctx.tracker
        if role == "open_pr":
            return tracker.pr_by_key(self.ctx.run_id, key) or self._existing(role, args, key)
        return tracker.comment_by_key(self.ctx.run_id, key)

    def _escalate(self, name: str, why: str) -> ToolResult:
        self._log(f"write {name} escalated: {why}")
        return ToolResult(ESCALATED, True, 0.0, "timeout", escalate=True)

    # -- the call record ------------------------------------------------------------------------
    def _record(self, name, args, outcome, is_error, result, start, plan_step, *, key=None, attempt=1) -> None:
        if self.ctx.calls is None:
            return
        self.ctx.calls.write(tool=name, args=args, plan_step=plan_step, key=key, attempt=attempt, outcome=outcome,
                             is_error=is_error, result=result, latency_s=time.monotonic() - start, cost_usd=0.0)


def default_tools(ctx: RunContext) -> ToolRegistry:
    return ToolRegistry(ctx)
