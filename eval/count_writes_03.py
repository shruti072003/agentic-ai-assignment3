"""Assignment 3, Part D, task 4: count each arm's writes from the tracker, not from traces.

    python -m eval.count_writes_03            # prints the table; fills reports/LAB_03.json's writes

The traces only say which runs belong to which arm (run_start.switches, checks/
FORMATS.md, section 11.9) and how each ended. Everything else is read from the
tracker's database, tracker/tracker.db, by run_id: the comments and PRs each run
left, and the tracker's own log of write attempts (its attempts table), whose
timeout_after_commit rows are the writes that timed out and had in fact committed.
Each of those is checked against the run's call record: a line with the same key
and outcome timeout must exist, or the harness never knew the write it made.

This script is evaluation, in eval/: the harness itself never reads the attempts table.
"""

from __future__ import annotations

import json
import sqlite3
import sys

import yaml

from eval.check_triage import check
from harness import LAB, ROOT
from harness.calls import read_calls
from harness.formats import UNFINISHED, parse_triage_comment
from harness.runner import is_free, is_trace
from harness.trace import read_trace
from harness.tracker import Tracker

ARMS = {"harness": {"flaky_writes": True, "readback_fail": False, "key_mode": "derived", "verify": "on"},
        "control": {"flaky_writes": True, "readback_fail": False, "key_mode": "uuid", "verify": "off"}}
DEFAULTS = {"flaky_writes": False, "readback_fail": False, "key_mode": "derived", "verify": "on"}


def arm_runs(answers: dict) -> dict[str, list[dict]]:
    arms = {a: [] for a in ARMS}
    for path in sorted((ROOT / "runs").glob("*.jsonl")):
        if not is_trace(path):
            continue
        records = read_trace(path)
        start = records[0] if records and records[0].get("type") == "run_start" else None
        if (not start or start.get("lab") != LAB or start.get("way") != "planned"
                or is_free(start.get("backend") or "") or start.get("issue") not in answers
                or records[-1].get("type") != "run_end"):
            continue
        ends = [r.get("terminal_reason") for r in records if r.get("type") == "end"]
        if set(ends) & set(UNFINISHED):
            continue
        switches = {**DEFAULTS, **(start.get("switches") or {})}
        for arm, wanted in ARMS.items():
            if switches == wanted:
                arms[arm].append({"run_id": start["run_id"], "issue": start["issue"], "sweep": start["sweep"],
                                  "end": ends[-1] if ends else None})
    return arms


def tally(db, reader: Tracker, run: dict, answer: dict) -> dict:
    rid, issue = run["run_id"], run["issue"]
    comments = [dict(r) for r in db.execute("SELECT * FROM comments WHERE run_id = ? AND issue = ? ORDER BY id",
                                            (rid, issue))]
    prs = [dict(r) for r in db.execute("SELECT * FROM prs WHERE run_id = ? ORDER BY id", (rid,))]
    file = parse_triage_comment(comments[0]["body"])[1] if comments else None
    right = [p for p in prs if p["branch"] == f"repomind/issue-{issue}" and p["state"] == "open"]
    want = 1 if file not in (None, "none") else 0
    committed = [dict(r) for r in db.execute("SELECT * FROM attempts WHERE run_id = ? AND outcome = "
                                             "'timeout_after_commit'", (rid,))]
    calls = read_calls(ROOT / "runs" / f"{rid}.calls.jsonl")
    timed_out_keys = {c["key"] for c in calls if c.get("outcome") == "timeout" and c.get("key")}
    return {"comments": len(comments), "prs": len(prs), "open_from_branch": len(right), "file": file,
            "prs_ok": len(right) == want and len(prs) == len(right), "committed_timeouts": len(committed),
            "committed_unrecorded": sum(1 for a in committed if a["key"] not in timed_out_keys),
            "passes": check(reader, rid, issue, answer)}


def main() -> int:
    answers = {int(k): v for k, v in yaml.safe_load((ROOT / "fixtures" / "answers.yaml").read_text())["eval"].items()}
    db = sqlite3.connect(ROOT / "tracker" / "tracker.db")
    db.row_factory = sqlite3.Row
    reader = Tracker(ROOT, flaky=False, readback_fail=False)
    writes, rows = {}, {}
    for arm, runs in arm_runs(answers).items():
        t = [dict(run, **tally(db, reader, run, answers[run["issue"]])) for run in runs]
        rows[arm] = t
        writes[arm] = {"runs": len(t), "comments_per_run_max": max((r["comments"] for r in t), default=0),
                       "duplicate_comment_runs": sum(r["comments"] > 1 for r in t),
                       "prs_correct_runs": sum(r["prs_ok"] for r in t),
                       "committed_timeouts": sum(r["committed_timeouts"] for r in t),
                       "escalated": sum(r["end"] == "escalated" for r in t),
                       "triage_passes": sum(r["passes"] for r in t)}
    db.close()

    for arm, t in rows.items():
        print(f"\n{arm}: {len(t)} runs")
        for r in sorted(t, key=lambda r: (r["sweep"], r["issue"])):
            print(f"  {r['run_id']:<30} end {str(r['end']):<10} comments {r['comments']} PRs {r['prs']} "
                  f"(open from branch {r['open_from_branch']}, right {r['prs_ok']}) file {r['file']} "
                  f"committed timeouts {r['committed_timeouts']} (not in call record {r['committed_unrecorded']}) "
                  f"pass {r['passes']}")
    h, c = writes.get("harness", {}), writes.get("control", {})

    def per_run(arm_rows, key):
        values = [r[key] for r in arm_rows]
        return f"mean {sum(values) / len(values):.2f}, min {min(values)}, max {max(values)}" if values else "n/a"

    print("\n| Measure | Your harness | Control |\n|---|---|---|")
    print(f"| Triage comments per run (should be 1) | {per_run(rows['harness'], 'comments')} | "
          f"{per_run(rows['control'], 'comments')} |")
    print(f"| Open PRs from `repomind/issue-<n>` per run | {per_run(rows['harness'], 'open_from_branch')}; "
          f"{h.get('prs_correct_runs')} of {h.get('runs')} runs right | {per_run(rows['control'], 'open_from_branch')}; "
          f"{c.get('prs_correct_runs')} of {c.get('runs')} runs right |")
    print(f"| Writes that timed out and had in fact committed | {h.get('committed_timeouts')} | "
          f"{c.get('committed_timeouts')} |")
    print(f"| Runs that ended `escalated` | {h.get('escalated')} of {h.get('runs')} | "
          f"{c.get('escalated')} of {c.get('runs')} |")
    print(f"| Triage success (grounded check) | {h.get('triage_passes')} of {h.get('runs')} | "
          f"{c.get('triage_passes')} of {c.get('runs')} |")
    unrecorded = sum(r["committed_unrecorded"] for t in rows.values() for r in t)
    print(f"\ncommitted timeouts with no matching timeout line in the call record: {unrecorded}")

    report_path = ROOT / "reports" / "LAB_03.json"
    report = json.loads(report_path.read_text())
    report["writes"] = writes
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print("\nwrote reports/LAB_03.json writes:", json.dumps(writes))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
