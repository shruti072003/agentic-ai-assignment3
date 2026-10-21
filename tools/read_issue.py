"""issues_read: return one issue from the tracker. Untrusted content: anyone can file an issue.

Its definition, the only thing the model sees, is read_issue.json. The result is
JSON, with the issue's semantic identifier (issue-4192) beside its number.

Failures (Part B): transient, none of its own (the registry retries a read the
file system or database refuses for a moment); permanent, a number the tracker
does not hold; ambiguous, none: a read changes nothing.
"""

import json

MAX_BODY_CHARS = 8_000


def validate(ctx, args: dict) -> list[str]:
    """Content rules the schema cannot hold (strict mode allows no minimum)."""
    if args["number"] < 1:
        return [f"number: {args['number']} is not an issue number; issue numbers are positive integers, such as 4192"]
    return []


def run(ctx, number):
    from harness.tracker import IssueNotFound
    from tools import ToolError

    try:
        issue = ctx.tracker.issue(int(number))
    except IssueNotFound:
        held = ", ".join(str(n) for n in ctx.tracker.issue_numbers("eval") + ctx.tracker.issue_numbers("dev"))
        raise ToolError(f"No issue {number} in the tracker. The numbers it holds: {held}. "
                        "This will not succeed with the same number; use the number the task gave you.")
    body = str(issue.get("body") or "")
    return json.dumps({
        "issue": f"issue-{issue['number']}", "number": int(issue["number"]), "title": str(issue["title"]),
        "author": str(issue["author"]), "opened": str(issue["opened"]),
        "body": body[:MAX_BODY_CHARS], "truncated": len(body) > MAX_BODY_CHARS,
    })
