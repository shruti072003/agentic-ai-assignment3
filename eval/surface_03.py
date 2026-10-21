"""Assignment 3, Part D, task 1: what the tool surface costs per call and per run.

    python -m eval.surface_03 count     # a few tiny model calls; writes eval/surface_counts.json
    python -m eval.surface_03 price     # no model calls; prices a mean harness-arm run, fills reports/LAB_03.json

count. Counts by difference, as harness/ping.py builds the client: the same
one-message call with and without what is being counted, the difference in
prompt_tokens (cached tokens included). For the starter's definitions (the SENT
fields each file has in the repository's first commit, no examples) and for ours
(as the registry sends them, registry.schemas): the five together, each alone, and
the planner's tool list (agent.plan._tool_list) added to the message. A definition
counted alone includes the vendor's own preamble for tool use, which any call
carrying a tool pays once.

price. Over the harness arm's finished runs (checks/FORMATS.md, section 11.9), each
model_call line is charged at the model's uncached input rate for what it carried
(its `tools`): a fill or rewrite call for its one tool, counted alone; a plan or
revise call for the tool list (submit_plan is the same before and after, so it is
left out); a call with no tool, nothing. The same calls are priced three ways: with
the starter's counts, with ours, and as if every call, the planner's and reviser's
included, had carried all five of ours and nothing else.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from agent.plan import _tool_list
from harness import LAB, ROOT
from harness.backends import make_backend
from harness.client import ModelClient
from harness.pricing import PriceSheet
from harness.roles import ROLES, roles
from harness.runner import is_free, is_trace
from harness.settings import load_settings
from harness.trace import read_trace

STARTER_COMMIT = "25af70e"   # the repository's first commit: the starter's definitions
STARTER_NAMES = {"read_issue": "read_issue", "search": "search_repo", "read_file": "read_file",
                 "comment": "post_comment", "open_pr": "open_pr"}
MODULES = ("read_issue", "search_repo", "read_file", "post_comment", "open_pr")
SENT = ("name", "description", "input_schema", "strict")
COUNTS = ROOT / "eval" / "surface_counts.json"
MESSAGE = "Do not call any tool. Answer with the word ok."
HARNESS_ARM = {"flaky_writes": True, "readback_fail": False, "key_mode": "derived", "verify": "on"}


def starter_schemas() -> list[dict]:
    out = []
    for module in MODULES:
        text = subprocess.run(["git", "show", f"{STARTER_COMMIT}:tools/{module}.json"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout
        d = json.loads(text)
        out.append({k: d[k] for k in SENT if k in d})
    return out


def our_schemas() -> list[dict]:
    from tools import RunContext, default_tools
    return default_tools(RunContext(run_id="surface", tracker=None, repo_root=ROOT / "fixtures" / "repo")).schemas


def count(argv=None) -> dict:
    settings = load_settings(ROOT / "settings.yaml")
    client = ModelClient(make_backend(settings.provider, settings), PriceSheet(ROOT / "pricing.yaml"))

    def prompt(tools=None, extra="") -> int:
        response = client.create(messages=[{"role": "user", "content": MESSAGE + extra}], tools=tools)
        return response.usage.prompt_tokens

    base = prompt()
    out = {"model": settings.model, "baseline_prompt_tokens": base}
    for label, schemas in (("before", starter_schemas()), ("after", our_schemas())):
        alone = {s["name"]: prompt([s]) - base for s in schemas}
        out[label] = {"five": prompt(schemas) - base, "alone": alone,
                      "list": prompt(extra="\n\nThe tools the plan may use:\n" + _tool_list(schemas))
                      - prompt(extra="\n\nThe tools the plan may use:\n"),
                      "names": [s["name"] for s in schemas]}
    out["calls"] = client.totals()
    COUNTS.write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))
    return out


def harness_runs() -> list[list[dict]]:
    answers = {4201, 4202, 4203, 4204, 4205, 4206}
    runs = []
    for path in sorted((ROOT / "runs").glob("*.jsonl")):
        if not is_trace(path):
            continue
        records = read_trace(path)
        start = records[0] if records else {}
        if (start.get("type") != "run_start" or start.get("lab") != LAB or start.get("way") != "planned"
                or is_free(start.get("backend") or "") or start.get("issue") not in answers
                or records[-1].get("type") != "run_end"):
            continue
        if {**HARNESS_ARM, **(start.get("switches") or {})} != HARNESS_ARM or start.get("switches") is None:
            continue
        if set(r.get("terminal_reason") for r in records if r.get("type") == "end") & {"quota", "rate_limited"}:
            continue
        runs.append(records)
    return runs


def price(argv=None) -> dict:
    counts = json.loads(COUNTS.read_text())
    settings = load_settings(ROOT / "settings.yaml")
    rate = PriceSheet(ROOT / "pricing.yaml").find(settings.model).input
    ours = {entry["tool"]: role for role, entry in roles().items()}
    starter_alone = {role: counts["before"]["alone"][STARTER_NAMES[role]] for role in ROLES}
    our_alone = {role: counts["after"]["alone"][roles()[role]["tool"]] for role in ROLES}
    runs = harness_runs()
    if not runs:
        raise SystemExit("no finished harness-arm runs yet (Part D, task 3)")
    totals = {"before": 0, "after": 0, "all_five": 0}
    calls = 0
    kinds: dict[str, int] = {}
    for records in runs:
        for line in (r for r in records if r.get("type") == "model_call"):
            calls += 1
            carried = line.get("tools") or []
            purpose = line.get("purpose")
            kinds[purpose or "none"] = kinds.get(purpose or "none", 0) + 1
            if purpose in ("plan", "revise"):
                totals["before"] += counts["before"]["list"]
                totals["after"] += counts["after"]["list"]
            else:
                for name in carried:
                    if name in ours:
                        totals["before"] += starter_alone[ours[name]]
                        totals["after"] += our_alone[ours[name]]
            totals["all_five"] += counts["after"]["five"]
    n = len(runs)
    per_run = {k: v / n for k, v in totals.items()}
    surface = {
        "tokens_before": counts["before"]["five"], "tokens_after": counts["after"]["five"],
        "list_tokens_before": counts["before"]["list"], "list_tokens_after": counts["after"]["list"],
        "calls_per_run": round(calls / n, 2),
        "cost_per_run_before": round(per_run["before"] * rate / 1e6, 6),
        "cost_per_run_after": round(per_run["after"] * rate / 1e6, 6),
        "cost_per_run_all_five": round(per_run["all_five"] * rate / 1e6, 6),
    }
    report_path = ROOT / "reports" / "LAB_03.json"
    report = json.loads(report_path.read_text())
    report["surface"] = surface
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{n} harness-arm runs, {calls} model calls ({calls / n:.2f} per run), by purpose {kinds}")
    print(f"input rate ${rate}/M uncached")
    for k in totals:
        print(f"  {k:<9} {totals[k]:>8,} definition tokens in all, {per_run[k]:>9,.1f} per run, "
              f"${per_run[k] * rate / 1e6:.6f} per run")
    print(json.dumps(surface, indent=1))
    return surface


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    {"count": count, "price": price}[sys.argv[1] if len(sys.argv) > 1 else "count"](sys.argv[2:])
