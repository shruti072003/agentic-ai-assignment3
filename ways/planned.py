"""Assignment 2's working solution, Parts B, C and D: plan, validate, execute inside the
boundary, revise at one checkpoint, and a second pass before posting.
It finds each tool by its role in tools/roles.yaml (harness.roles), so a renamed
tool still runs, and it reads a search result as text or as JSON
(harness.formats.result_paths).

Assignment 3, Part C, task 1: the plan rule is exactly one comment step, the last,
with exactly one PR step right before it. A PR can be closed; a comment, once read,
cannot. When the executor reaches the PR step it first drafts the comment and runs
the second pass on it, so the PR knows the triage's file; a draft the second pass
holds, or one the comment tool's boundary would refuse, ends the run before either
write. A draft whose file is none skips the PR: a check here, in code, not a plan
condition or a revision. The comment step then posts that same draft. A write the
registry cannot confirm (result.escalate, the third exit) ends the run escalated,
and nothing is written after it. A filled call the registry refuses or fails
(rejected, or a tool error such as a near-miss path) gets one repair: the model is
shown the error, whose repair fact says what would be right, and fills the step again.

The boundary: the executor runs the plan's steps in order and nothing else. Where a
step's arguments are "$fill", one model call fills them, offered that step's tool
alone; a call to any other tool is refused, traced as off_plan, and never runs.
Two tries without an on-plan call halt the run as escalated.

The planner's output is checked against agent/plan_schema.json (harness.schema)
before the four checks, and so is the rule that its last step is the comment: a
vendor can accept "strict" and not enforce it, and a plan with no comment step
passes all four checks and posts nothing. A plan that breaks either is rejected
like one that fails a check.

The checkpoint, a check in code: after the first search step, and after any search
step a revision added or changed, until MAX_REVISIONS is spent, the search returned
at least one source file: a .ts or .tsx file under src/, not only documentation
(README.md, CHANGELOG.md, docs/) or translations (src/i18n/*.json). A revision never
changes a step that is done; it may change a failed step, which then runs again.

Part G's ways/notes.py extends this file without changing it: it subclasses
Executor and overrides its hooks, record() (after every step), context() (what
every executor call sees) and the class attribute critique, and asks its questions
once run() has returned, however the run ended.

The second pass, before the comment, in ctx.critique's mode: external runs
agent.critique.verify, code; self asks the model with agent.critique.self_critique.
A failing draft with a round left is rewritten by one call; a draft still failing
in the last round is held, and the run escalates. One comment, after the last round.
"""

from __future__ import annotations

import copy
import json
import re
from types import SimpleNamespace

from agent.critique import self_critique, verify
from agent.diff import step_args
from agent.plan import SCHEMA, make_plan, revise_plan
from agent.validator import validate
from harness.formats import FILL, format_triage_comment, parse_triage_comment, result_paths
from harness.roles import ROLES, arg, tool_for
from harness.schema import errors as schema_errors

MAX_REPLANS = 2       # three planner calls in all; a fourth on the same request is the same coin, so a person looks
MAX_REVISIONS = 2     # a repair of the first search, and one more if the repair also finds nothing
MAX_ROUNDS = 2        # check, rewrite once, check again: slide 43's limit, the same in both modes
MAX_FILL_TRIES = 2    # a step the model will not fill on plan, twice, halts the run
CHECKPOINT = "after_first_search"
SOURCE = re.compile(r"^src/(?!i18n/).+\.tsx?$")   # code, not documentation or translations
CONTEXT_CHARS = 6000  # of each earlier result, in a fill call


def role(name: str | None) -> str | None:
    """The role a step's tool plays (tools/roles.yaml), or None."""
    return next((r for r in ROLES if tool_for(r) == name), None)


def open_args(step: dict) -> dict:
    """A step's arguments, with any string that holds "$fill" read as "$fill": a planner that
    writes "label: $fill" or "file:$fill" means a value the executor fills in, not that text."""
    return {k: FILL if isinstance(v, str) and FILL in v else v for k, v in step_args(step).items()}


def allows_null(spec: dict) -> bool:
    """Whether a property's schema takes null: "type": [..., "null"], or a null branch."""
    kind = spec.get("type")
    return ("null" in kind if isinstance(kind, list) else kind == "null") or any(
        isinstance(b, dict) and allows_null(b) for b in spec.get("anyOf") or spec.get("oneOf") or [])


def run(ctx) -> None:
    plan = plan_until_valid(ctx)
    if plan is None:
        return
    ctx.trace.plan(1, plan)
    Executor(ctx, plan).run()


def schema_check(ctx, plan: dict, version: int) -> list[dict]:
    """The plan as the planner's tool returned it, against agent/plan_schema.json. A
    failure is written as a schema line, and comes back as failures for a re-plan."""
    problems = schema_errors({k: v for k, v in plan.items() if k != "meta"}, SCHEMA)
    steps = plan.get("steps") or []
    roles_ = [role(s.get("tool")) if isinstance(s, dict) else None for s in steps]
    if not steps or roles_[-1] != "comment" or roles_.count("comment") != 1:
        problems.append(f"the plan needs exactly one comment step, {tool_for('comment')}, and it must be the last "
                        "step: the comment comes last")
    if roles_.count("open_pr") != 1 or len(roles_) < 2 or roles_[-2] != "open_pr":
        problems.append(f"the plan needs exactly one pull request step, {tool_for('open_pr')}, right before the "
                        "comment step")
    if problems:
        ctx.trace.event("schema", version=version, errors=problems[:20],
                        tools=[s.get("tool") if isinstance(s, dict) else None for s in steps])
    return [{"step": None, "check": "schema", "error": e} for e in problems[:5]]


def plan_until_valid(ctx) -> dict | None:
    """The reject exit: re-plan up to MAX_REPLANS times, then escalate. Never execute a rejected plan."""
    failures = None
    for attempt in range(1, MAX_REPLANS + 2):
        plan = make_plan(ctx.client, ctx.issue_number, ctx.tools.schemas, trace=ctx.trace, failures=failures)
        broken = schema_check(ctx, plan, 1)
        if broken:                                # rejected before the four checks: it never runs
            failures = broken
            continue
        verdict = validate(plan)
        ctx.trace.event("validation", version=1, attempt=attempt, valid=verdict["valid"],
                        failures=verdict["failures"])
        if verdict["valid"]:
            return plan
        failures = verdict["failures"]
    ctx.trace.end("escalated", detail=f"no valid plan in {MAX_REPLANS + 1} attempts; last failures {failures}")
    return None


class Executor:
    critique = True   # the notes way turns the second pass off

    def __init__(self, ctx, plan: dict):
        self.ctx = ctx
        self.plan = copy.deepcopy(plan)
        self.version = 1
        self.results: list[dict] = []    # step_results (checks/FORMATS.md, section 10.3), in the order steps ran
        self.outputs: dict[str, str] = {}  # each step's tool output, for later calls; never shown to the reviser
        self.calls = 0                   # execute and critique calls so far: the step number of the next lines
        self.revisions = 0
        self.revised: set[str] = set()   # ids of steps a revision added or changed: the checkpoint follows them
        self.ran: set[str] = set()       # ids of steps that ran and are not to run again
        self.drafted: dict | None = None # the comment's arguments, drafted and checked before either write

    # -- the plan, in order ------------------------------------------------------------
    def next_step(self) -> dict | None:
        return next((s for s in self.plan.get("steps") or [] if str(s.get("id")) not in self.ran), None)

    def run(self) -> None:
        while True:
            step = self.next_step()
            if step is None:
                return                            # the plan is finished; the runner writes completed
            if role(step.get("tool")) == "open_pr":
                if not self.open_pr(step):
                    return
                continue
            if role(step.get("tool")) == "comment":
                if not self.comment(step):
                    return
                continue
            first = not any(role(r["tool"]) == "search" for r in self.results)
            result = self.run_step(step)
            if result is None:
                return                            # halted, and ended escalated
            if role(step.get("tool")) == "search" and (first or str(step.get("id")) in self.revised):
                self.checkpoint(CHECKPOINT, any(SOURCE.match(p) for p in result.get("paths") or []))

    # -- one step ------------------------------------------------------------------------
    def run_step(self, step: dict) -> dict | None:
        tool, args = step.get("tool"), open_args(step)
        if not tool:
            return self.record(step, args, None, "no_tool")
        planned = args
        args = self.filled(step, args)
        if args is None:
            return None
        result = self.ctx.call_tool(self.calls, tool, args, plan_step=step.get("id"))
        to_fill = [k for k, v in planned.items() if v == FILL]
        if result.is_error and result.outcome in ("rejected", "error") and to_fill:
            repaired = self.fill(step, planned, to_fill, note=self.repair_note(args, result))
            if repaired is not None:          # one repair turn, from the error's repair fact
                args = repaired
                result = self.ctx.call_tool(self.calls, tool, args, plan_step=step.get("id"))
        return self.record(step, args, result)

    @staticmethod
    def repair_note(args: dict, result) -> str:
        return (f"Your last call, {json.dumps(args)}, was refused:\n{result.content}\n"
                "Call the tool again with the arguments the error says would be right.")

    def filled(self, step: dict, args: dict, note: str = "") -> dict | None:
        to_fill = [k for k, v in args.items() if v == FILL]
        if not to_fill:
            return args
        filled = self.fill(step, args, to_fill, note=note)
        if filled is None:
            self.record(step, args, None, "off_plan")
            self.ctx.trace.end("escalated", detail=f"step {step.get('id')}: no on-plan call in {MAX_FILL_TRIES} tries")
        return filled

    def fill(self, step: dict, args: dict, keys: list[str], *, system: str = "executor.md",
             purpose: str = "execute", note: str = "") -> dict | None:
        """One model call fills a step's "$fill" arguments, offered the step's tool alone. It
        keeps every argument the model gave that the schema knows, the ones the plan never
        mentioned included, and a null where the schema allows one (Part A's required and
        nullable fields); the plan's own values win."""
        schema = next((t for t in self.ctx.tools.schemas if t["name"] == step["tool"]), None)
        if schema is None:
            return None
        fixed = {k: v for k, v in args.items() if v != FILL}
        props = (schema.get("input_schema") or {}).get("properties") or {}
        nullable = {k for k, s in props.items() if isinstance(s, dict) and allows_null(s)}
        for _ in range(MAX_FILL_TRIES):
            self.calls += 1
            response = self.ctx.client.create(system=self.ctx.prompt(system), tools=[schema],
                                              messages=[{"role": "user", "content": self.fill_request(step, fixed, keys, note)}])
            self.ctx.trace.model_call(self.calls, response, purpose=purpose)
            for call in response.tool_calls:
                if call.name != step["tool"]:
                    self.refuse(step, call)       # the boundary: never run, always traced
                    continue
                filled = {**{k: v for k, v in call.input.items() if k in props}, **fixed}   # the plan's values win
                if any(k not in filled or filled[k] in ("", FILL) or (filled[k] is None and k not in nullable)
                       for k in keys):
                    continue
                if role(step["tool"]) == "comment":
                    body = arg("comment", "body")
                    label, file = parse_triage_comment(str(filled[body]))
                    filled[body] = format_triage_comment(label or "bug", file)
                return filled
        return None

    def refuse(self, step: dict, call) -> None:
        self.ctx.trace.event("off_plan", step=step.get("id"), tool=call.name, input=call.input, version=self.version)

    def context(self) -> list[str]:
        """What the run has gathered so far, one block per step, for a fill or a critique."""
        return [f"## Step {r['id']}, {r['tool']}, {r['status']}\n{self.outputs.get(str(r['id']), '')[:CONTEXT_CHARS]}"
                for r in self.results]

    def fill_request(self, step: dict, fixed: dict, keys: list[str], note: str = "") -> str:
        parts = [f"Triage issue #{self.ctx.issue_number}. You are running step {step.get('id')} of a fixed "
                 f"plan: {step.get('action')}. Call {step['tool']} once. Fill in: {', '.join(keys)}. "
                 f"The plan fixes: {json.dumps(fixed)}."]
        if role(step["tool"]) == "comment":
            parts.append("The comment's first two lines must be:\nlabel: <bug | feature | question | docs>\n"
                         "file: <path relative to the repository root, or none>")
        if role(step["tool"]) == "open_pr":
            parts.append("The pull request is a draft that proposes, in prose, the change to the file the triage "
                         "names. The title is one line; the body names the file exactly and says what to change.")
        if note:
            parts.append(note)
        return "\n\n".join(parts + self.context())

    def record(self, step: dict, args: dict, result, error: str | None = None) -> dict:
        tool = step.get("tool")
        text = result.content if result is not None else ""
        count, paths = None, []
        if result is not None and role(tool) in ("search", "read_file"):
            paths = result_paths(tool, args, text, result.is_error)
            count = len(paths) if role(tool) == "search" and not result.is_error else None
        if error is None and result is not None and result.is_error:
            error = "not_found" if "no such file" in text else "tool_error"
        if error is None and role(tool) == "search" and not count:
            error = "no_matches"
        entry = {"id": step.get("id"), "tool": tool, "status": "failed" if error else "done", "count": count,
                 "paths": paths, "error_code": error, "args": args}
        self.results.append(entry)
        self.ran.add(str(step.get("id")))
        self.outputs[str(step.get("id"))] = text
        for s in self.plan.get("steps") or []:
            if str(s.get("id")) == str(step.get("id")):
                s["status"] = entry["status"]
        return entry

    # -- revision, at the checkpoint whose check failed --------------------------------------
    def checkpoint(self, name: str, ok: bool) -> bool:
        """True when a new version is now active."""
        if ok or self.revisions >= MAX_REVISIONS:
            return False
        self.revisions += 1
        proposed = revise_plan(self.ctx.client, self.plan, self.results, trace=self.ctx.trace)
        broken = schema_check(self.ctx, proposed, self.version + 1)
        verdict = {"valid": False, "failures": broken} if broken else validate(proposed)
        self.ctx.trace.event("revision", checkpoint=name, version=self.version + 1, valid=verdict["valid"],
                             failures=verdict["failures"])
        if not verdict["valid"]:
            return False                          # rejected: it never runs, and it counted against the bound
        before = {str(s.get("id")): {k: v for k, v in s.items() if k != "status"} for s in self.plan.get("steps") or []}
        changed = {str(s.get("id")) for s in proposed.get("steps") or []
                   if before.get(str(s.get("id"))) != {k: v for k, v in s.items() if k != "status"}}
        self.revised |= changed
        failed = {str(r["id"]) for r in self.results if r["status"] == "failed"}
        done = {str(r["id"]) for r in self.results if r["status"] == "done"}
        self.ran -= (changed & failed) - done      # a failed step the revision changed runs again
        self.version += 1
        self.plan = proposed
        self.ctx.trace.plan(self.version, proposed)
        return True

    # -- the draft, before either write ------------------------------------------------------------
    def comment_step(self) -> dict | None:
        return next((s for s in self.plan.get("steps") or [] if role(s.get("tool")) == "comment"), None)

    def draft_problem(self, step: dict, args: dict) -> str | None:
        """What the comment tool's boundary would refuse in this draft, or a file it names that
        does not exist: checked now, so that a refusal never comes after the PR."""
        from tools.post_comment import file_problem
        check = getattr(self.ctx.tools, "check_args", None)
        problem = check(step["tool"], args) if check else None
        if problem is None:
            problem = file_problem(SimpleNamespace(repo_root=self.ctx.root / "fixtures" / "repo"),
                                   str(args.get(arg("comment", "body"), "")))
        return problem

    def draft(self) -> dict | None:
        """The comment's arguments, drafted, checked and through the second pass, before
        either write; the same draft is posted at the comment step. None: the run has ended."""
        if self.drafted is not None:
            return self.drafted
        step = self.comment_step()
        if step is None:
            return None
        planned = open_args(step)
        draft = self.filled(step, planned)
        if draft is None:
            return None
        problem = self.draft_problem(step, draft)
        if problem is not None:               # one repair, from the refusal's repair fact
            body = arg("comment", "body")
            seen = list(dict.fromkeys(p for r in self.results for p in r.get("paths") or []))
            redo = self.fill(step, {**draft, body: FILL}, [body],
                             note=f"Your draft was refused: {problem}\n\nThe draft:\n{draft[body]}\n"
                                  + (f"The files this run has seen: {', '.join(seen[:30])}.\n" if seen else "")
                                  + "Fix what the refusal names and nothing else.")
            problem = self.draft_problem(step, redo) if redo is not None else problem
            if problem is not None:
                self.record(step, draft, None, "draft_refused")
                self.ctx.trace.end("escalated", detail=f"the draft comment would be refused: {problem}"[:500])
                return None
            draft = redo
        final = self.second_pass(step, draft) if self.critique else draft
        if final is None:
            self.record(step, draft, None, "held")
            self.ctx.trace.end("escalated", detail=f"the draft still failed the second pass after {MAX_ROUNDS} rounds")
            return None
        self.drafted = final
        return final

    # -- the pull request, then the comment -------------------------------------------------------
    def open_pr(self, step: dict) -> bool:
        """Draft the comment, then open the PR for the file it names, or skip the PR when it
        names none. False when the run has ended."""
        draft = self.draft()
        if draft is None:
            return False
        _, file = parse_triage_comment(str(draft[arg("comment", "body")]))
        if file in (None, "none"):
            entry = self.record(step, open_args(step), None, "skipped_no_file")
            entry["status"] = "skipped"
            for s in self.plan.get("steps") or []:
                if str(s.get("id")) == str(step.get("id")):
                    s["status"] = "skipped"
            self.ctx.trace.event("pr_skipped", step=step.get("id"), reason="the draft comment names no file")
            return True
        issue = self.ctx.issue_number
        planned = {**open_args(step), arg("open_pr", "number"): issue, arg("open_pr", "branch"): f"repomind/issue-{issue}"}
        note = (f"The triage comment is drafted, and the pull request must match it:\n\n{draft[arg('comment', 'body')]}"
                f"\n\nThe file to change is {file}.")
        args = self.filled(step, planned, note=note)
        if args is None:
            return False
        result = self.write_pr(step, args, file)
        to_fill = [k for k, v in planned.items() if v == FILL]
        if result.is_error and not result.escalate and result.outcome in ("rejected", "error") and to_fill:
            redo = self.fill(step, planned, to_fill, note=note + "\n\n" + self.repair_note(args, result))
            if redo is not None:              # one repair turn
                args, result = redo, self.write_pr(step, redo, file)
        self.record(step, args, result)
        if result.escalate:
            self.ctx.trace.end("escalated", detail="the pull request's write could not be confirmed (third exit); "
                                                   "no comment was posted")
            return False
        if result.is_error:
            self.ctx.trace.end("escalated", detail=f"the pull request could not be opened: {result.content}"[:500])
            return False
        return True

    def write_pr(self, step: dict, args: dict, file: str):
        body = arg("open_pr", "body")
        if file not in str(args.get(body, "")):   # the PR's body names the triage's file, always
            args[body] = f"File: {file}\n\n{args.get(body, '')}".strip()
        return self.ctx.call_tool(self.calls, step["tool"], args, plan_step=step.get("id"))

    def comment(self, step: dict) -> bool:
        """Post the draft, once, last. False when the run has ended."""
        draft = self.draft()
        if draft is None:
            return False
        result = self.ctx.call_tool(self.calls, step["tool"], draft, plan_step=step.get("id"))
        self.record(step, draft, result)
        if result.escalate:
            self.ctx.trace.end("escalated", detail="the comment's write could not be confirmed (third exit)")
            return False
        if result.is_error:
            self.ctx.trace.end("escalated", detail=f"the comment could not be posted: {result.content}"[:500])
            return False
        return True

    def evidence(self) -> dict:
        issue = next((self.outputs.get(str(r["id"]), "") for r in self.results if role(r["tool"]) == "read_issue"), "")
        return {"issue": issue, "results": self.context()}

    def second_pass(self, step: dict, args: dict) -> dict | None:
        mode = self.ctx.critique
        if mode not in ("self", "external"):
            return args
        body = arg("comment", "body")
        for round_ in range(1, MAX_ROUNDS + 1):
            draft = args[body]
            if mode == "external":
                verdict = verify(draft, self.ctx)
            else:
                self.calls += 1
                v = self_critique(self.ctx.client, draft, self.evidence(), self.ctx.trace, step=self.calls)
                verdict = {"passed": v["decision"] == "accept", "reasons": [v["reason"]] if v.get("reason") else []}
            reason = "; ".join(verdict["reasons"]) or None
            changed = False
            if not verdict["passed"] and round_ < MAX_ROUNDS:
                rewritten = self.rewrite(step, args, reason or "the critic asked for a revision")
                if rewritten is not None:
                    changed = rewritten[body] != draft
                    args = rewritten
            self.ctx.trace.event("critique", round=round_, mode=mode, result="pass" if verdict["passed"] else "fail",
                                 reason=reason, changed=changed, draft=draft)
            if verdict["passed"]:
                return args
        return None                               # still failing in the last round: held

    def rewrite(self, step: dict, args: dict, reason: str) -> dict | None:
        """One model call rewrites the draft with the reason, offered the comment tool alone."""
        body = arg("comment", "body")
        note = f"Rewrite the draft. The draft:\n\n{args[body]}\n\nWhy it must change: {reason}"
        return self.fill(step, {**args, body: FILL}, [body], system="rewrite.md", purpose="critique", note=note)
