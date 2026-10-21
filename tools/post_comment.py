"""issues_post_comment: write a comment on an issue. Public: whoever filed the issue can read it.

Its definition is post_comment.json. `idempotency_key` is not in the definition:
the model never sees or sets it. The registry derives it and passes it (Assignment
3, Part C, task 2), and the tracker accepts each key once.

Failures (Part B): transient, a write refused before it was sent (none here: the
tracker's own timeouts are all after dispatch); permanent, an issue that does not
exist, a body that is empty, too long or not a triage; ambiguous, a write that timed
out after dispatch (TrackerTimeout): it may have committed. The registry, not this
module, decides what to do with a timeout: read it back, retry with the same key,
or escalate.
"""

import json

MAX_BODY_CHARS = 4_000


def validate(ctx, args: dict) -> list[str]:
    from harness.formats import LABELS, parse_triage_comment

    body = args["body"]
    if not body.strip():
        return ["body: it is empty; its first two lines must be label: <bug | feature | question | docs> and "
                "file: <path, or none>"]
    out = []
    if len(body) > MAX_BODY_CHARS:
        out.append(f"body: it is {len(body):,} characters long; the cap is {MAX_BODY_CHARS:,}")
    if args["number"] < 1:
        out.append(f"number: {args['number']} is not an issue number; use the issue being triaged, such as 4192")
    label, file = parse_triage_comment(body)
    if label not in LABELS:
        out.append(f"body: line 1 must be label: <{' | '.join(LABELS)}>"
                   + (f"; it says {label!r}" if label else "; there is no label: line"))
    if file is None:
        out.append("body: line 2 must be file: <the exact repository path, or none>; there is no file: line")
    return out


def file_problem(ctx, body: str) -> str | None:
    """Whether the file a triage names exists, case included: the planned way checks a
    draft with it before either write. Not a boundary rule, so a comment on a file that
    has since moved can still be posted by hand."""
    from harness.formats import parse_triage_comment
    from tools.read_file import near_misses, repo_files

    _, file = parse_triage_comment(body)
    if file in (None, "none"):
        return None
    files = repo_files(ctx.repo_root)
    if file in files:
        return None
    near = near_misses(file, files)
    return (f"body: file: {file} is not a file in the repository (paths are case-sensitive)"
            + (f"; nearest existing paths: {', '.join(near)}" if near else "")
            + ". Name a file exactly as a search printed it, or none")


def run(ctx, number, body, idempotency_key=None):
    from harness.tracker import IssueNotFound
    from tools import ToolError

    if not str(body).strip():
        raise ToolError("body is empty; nothing was posted.")
    try:
        comment = ctx.tracker.post_comment(ctx.run_id, int(number), str(body), key=idempotency_key)
    except IssueNotFound:
        raise ToolError(f"No issue {number} in the tracker; nothing was posted. This will not succeed with the "
                        "same number: comment on the issue being triaged.")
    return result(comment)


def result(comment: dict, replayed: bool | None = None) -> str:
    """The JSON the model gets, for a comment the tracker holds: new, or found again."""
    again = comment.get("replayed") if replayed is None else replayed
    return json.dumps({"comment": f"comment-{comment['id']}", "issue": f"issue-{comment['issue']}",
                       "status": "already_posted" if again else "posted"})
