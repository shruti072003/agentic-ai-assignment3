"""pulls_open_draft: open a draft pull request on the mock remote. New in Assignment 3.

Its definition is open_pr.json. The mock remote needs no commits: a PR is a branch,
a title and a body, stored in the tracker's database. `idempotency_key` is not in
the definition: the registry derives it and passes it, as for issues_post_comment.

Failures (Part B): transient, none of its own; permanent, an issue that does not
exist, a branch other than repomind/issue-<n>, an empty or overlong title or body;
ambiguous, a write that timed out after dispatch (TrackerTimeout). The registry
handles the timeout, as for the comment.
"""

import json

MAX_TITLE_CHARS = 120
MAX_BODY_CHARS = 4_000


def branch_for(number: int) -> str:
    return f"repomind/issue-{number}"


def validate(ctx, args: dict) -> list[str]:
    out = []
    number, branch, title, body = args["number"], args["branch"], args["title"], args["body"]
    if number < 1:
        out.append(f"number: {number} is not an issue number; use the issue being triaged, such as 4192")
    elif branch != branch_for(number):
        out.append(f"branch: {branch!r} is not this issue's branch; it must be exactly {branch_for(number)}")
    if not title.strip():
        out.append("title: it is empty; give one line saying what the change fixes or adds")
    elif len(title) > MAX_TITLE_CHARS or "\n" in title:
        out.append(f"title: it must be one line of at most {MAX_TITLE_CHARS} characters; it is {len(title)}"
                   + (" and has line breaks" if "\n" in title else ""))
    if not body.strip():
        out.append("body: it is empty; name the file to change and propose the change in prose")
    elif len(body) > MAX_BODY_CHARS:
        out.append(f"body: it is {len(body):,} characters long; the cap is {MAX_BODY_CHARS:,}")
    return out


def run(ctx, number, branch, title, body, idempotency_key=None):
    from harness.tracker import IssueNotFound
    from tools import ToolError

    try:
        pr = ctx.tracker.open_pr(ctx.run_id, int(number), str(branch), str(title), str(body), key=idempotency_key)
    except IssueNotFound:
        raise ToolError(f"No issue {number} in the tracker; nothing was opened. This will not succeed with the "
                        "same number: open the pull request for the issue being triaged.")
    except ValueError as exc:
        raise ToolError(f"{exc}; nothing was opened.")
    return result(pr)


def result(pr: dict, replayed: bool | None = None) -> str:
    """The JSON the model gets, for a PR the tracker holds: new, or found again."""
    again = pr.get("replayed") if replayed is None else replayed
    return json.dumps({"pr": f"pr-{pr['id']}", "issue": f"issue-{pr['issue']}", "branch": pr["branch"],
                       "status": "already_open" if again else "opened"})
