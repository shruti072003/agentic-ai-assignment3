"""code_read_file: return one file of the private codebase, capped, as JSON.

Its definition is read_file.json. It reads the same files code_search searches:
everything under fixtures/repo except config/. A path is relative to fixtures/repo,
as search results print it.

Assignment 3, Part B, task 3: it never asks the file system whether a path exists.
On a case-insensitive disk, which is macOS's default, opening src/report.ts opens
src/Report.ts and reports success. It compares the requested path, case included,
with the repository's file list, and a path not on the list is an error that names
the nearest paths that are.

Failures (Part B): transient, a read the file system refuses for a moment (the
registry retries it); permanent, a path that does not exist, a path under config/,
a start_line past the end, a file that is not text; ambiguous, none.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

MAX_CHARS = 20_000
MAX_LINES = {"concise": 150, "detailed": 600}
NEAR = 5          # near misses named, at most


def repo_files(repo_root) -> list[str]:
    """Every readable file, as a path relative to the repository root: the list a path is compared with."""
    from tools.search_repo import EXCLUDED_DIRS
    root = Path(repo_root)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and not (set(p.relative_to(root).parts) & EXCLUDED_DIRS))


def near_misses(rel: str, files: list[str]) -> list[str]:
    """The paths a model most likely meant: the same path in another case first, then the closest spellings."""
    same_case = [f for f in files if f.lower() == rel.lower()]
    scored = sorted(((difflib.SequenceMatcher(None, rel.lower(), f.lower()).ratio(), f) for f in files),
                    key=lambda t: (-t[0], t[1]))
    close = [f for score, f in scored if score >= 0.85 and f not in same_case]
    return (same_case + close)[:NEAR]


def validate(ctx, args: dict) -> list[str]:
    out = []
    path = args["path"].strip()
    if not path:
        out.append("path: it is empty; give a file's path as code_search printed it, such as src/api/router.ts")
    elif path.startswith("/") or ".." in path.replace("\\", "/").split("/"):
        out.append(f"path: {args['path']!r} leaves the repository; give a path relative to its root, such as "
                   "src/api/router.ts, with no leading / and no ..")
    elif len(path) > 300:
        out.append(f"path: it is {len(path)} characters long; a path here is at most 300")
    if args["start_line"] is not None and args["start_line"] < 1:
        out.append(f"start_line: {args['start_line']} is before the first line; use null, or a line number from 1")
    return out


def run(ctx, path, response_format, start_line):
    from harness.formats import normalize_path
    from tools import ToolError
    from tools.search_repo import EXCLUDED_DIRS

    rel = normalize_path(str(path))
    if rel in (None, "none"):
        raise ToolError("path is empty: give a file's path as code_search printed it, such as src/api/router.ts.")
    if set(rel.split("/")) & EXCLUDED_DIRS:
        raise ToolError(f"{rel} is not readable: config/ holds secrets and is outside what this tool reads. No "
                        "retry will succeed; read a file outside config/.")
    files = repo_files(ctx.repo_root)
    if rel not in files:
        near = near_misses(rel, files)
        folder = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
        siblings = [f for f in files if f.startswith(folder) and "/" not in f[len(folder):]][:12]
        raise ToolError(f"No such file: {rel} (paths are case-sensitive). "
                        + (f"Nearest existing paths: {', '.join(near)}. Retry with one of them exactly; "
                           if near else
                           f"No path is close to it. The files in {folder or 'the root'}: {', '.join(siblings)}. "
                           "Read one of them, or search with code_search for a word in the file you want; "
                           if siblings else "No path is close to it: search with code_search for a word in the file you want; ")
                        + "this path will not succeed.")
    try:
        lines = (Path(ctx.repo_root) / rel).read_text().splitlines()
    except UnicodeDecodeError:
        raise ToolError(f"{rel} is not a text file; no retry will succeed.")
    start = start_line or 1
    if lines and start > len(lines):
        raise ToolError(f"start_line {start} is past the end of {rel}, which has {len(lines)} lines; use null or "
                        f"a number from 1 to {len(lines)}.")
    cap = MAX_LINES[response_format]
    chunk = lines[start - 1:start - 1 + cap]
    if response_format == "detailed":
        chunk = [f"{start + i}| {line}" for i, line in enumerate(chunk)]
    content = "\n".join(chunk)
    cut = len(content) > MAX_CHARS
    if cut:                                       # whole lines only, so next_start_line is exact
        content = content[:MAX_CHARS].rsplit("\n", 1)[0]
        chunk = content.split("\n")
    end = start - 1 + len(chunk) if lines else 0
    truncated = end < len(lines)
    return json.dumps({"path": rel, "total_lines": len(lines), "start_line": start if lines else 0, "end_line": end,
                       "truncated": truncated, "next_start_line": end + 1 if truncated else None,
                       "content": content})
