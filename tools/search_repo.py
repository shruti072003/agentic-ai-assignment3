"""code_search: literal, case-insensitive text search over the private codebase.

Its definition is search_repo.json. It reads a prebuilt index,
fixtures/search_index/index.json, of every text file under fixtures/repo except
config/, and the index records when it was built. With source "live" it reads the
files themselves instead.

Assignment 3, Part A: the result is JSON, and nothing matched is told apart from
malformed in the raw response. A search that matches nothing is a success with
count 0 and query_valid true. A malformed search is any of three things, and each
is a tool execution error (the boundary refuses it before run(), through validate):
  1. a query that is empty or only whitespace;
  2. a query longer than MAX_QUERY_CHARS;
  3. a path_glob that is not a valid glob (glob_problem, below).
`truncated` and `index_age` are what the starter's last line reported: whether more
lines matched than were shown, and how old the index is.

Failures (Part B): transient, none of its own (the registry retries an index read
the file system refuses for a moment); permanent, a malformed query; ambiguous,
none: a read changes nothing.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

EXCLUDED_DIRS = {"config", "node_modules", ".git"}
HARD_CAP = 20
MAX_QUERY_CHARS = 200
INDEX = "fixtures/search_index/index.json"
GLOB_CHARS = re.compile(r"^[A-Za-z0-9_.\-/*?\[\]{},]+$")
EXCERPT = {"concise": 120, "detailed": 240}
BY_FILE = 10      # files named in a truncated result's by_file, most matches first


def glob_problem(pattern: str) -> str | None:
    """Why `pattern` is not a valid path glob, or None when it is one."""
    if pattern.startswith("/"):
        return "a glob is relative to the repository root; it cannot start with /"
    if ".." in pattern.split("/"):
        return "a glob cannot leave the repository (..)"
    if not GLOB_CHARS.match(pattern):
        return "a glob uses letters, digits, / . _ - and the wildcards * ? [ ] { }; no spaces, colons or quotes"
    depth = 0
    for ch in pattern:
        depth += {"[": 1, "]": -1}.get(ch, 0)
        if depth not in (0, 1):
            return "a glob's [ and ] must pair up"
    if depth:
        return "a glob's [ and ] must pair up"
    if pattern.count("{") != pattern.count("}"):
        return "a glob's { and } must pair up"
    return None


def validate(ctx, args: dict) -> list[str]:
    """The content rules: the three kinds of malformed search, and max_results's range."""
    out = []
    query = args["query"]
    if not query.strip():
        out.append("query: it is empty; give the literal text to find, 1 to 200 characters, such as an identifier "
                   "(backoffDelay) or one phrase of an error message")
    elif len(query) > MAX_QUERY_CHARS:
        out.append(f"query: it is {len(query):,} characters long; the cap is {MAX_QUERY_CHARS}. Search for one "
                   "identifier or one short phrase from it")
    if args["path_glob"] is not None:
        why = glob_problem(args["path_glob"]) if args["path_glob"].strip() else "it is empty"
        if why:
            out.append(f"path_glob: {args['path_glob']!r} is not a valid glob: {why}. Use null to search every file, "
                       "or a glob such as src/api/** or **/*.tsx")
    if not 1 <= args["max_results"] <= HARD_CAP:
        out.append(f"max_results: {args['max_results']} is out of range; it is 1 to {HARD_CAP}, usually 10")
    return out


def _expand(pattern: str) -> list[str]:
    """{a,b} alternatives, which fnmatch does not know."""
    match = re.search(r"\{([^{}]*)\}", pattern)
    if not match:
        return [pattern]
    return [p for option in match.group(1).split(",")
            for p in _expand(pattern[:match.start()] + option + pattern[match.end():])]


def _matches(path: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    return any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(path, p.replace("**/", ""))
               for p in _expand(pattern))


def load_index(root: Path) -> dict:
    return json.loads((Path(root) / INDEX).read_text())


def _disk(repo_root: Path) -> dict[str, list[str]]:
    files = {}
    for path in sorted(Path(repo_root).rglob("*")):
        rel = path.relative_to(repo_root)
        if path.is_file() and not (set(rel.parts) & EXCLUDED_DIRS):
            try:
                files[rel.as_posix()] = path.read_text().splitlines()
            except UnicodeDecodeError:
                continue
    return files


def age(built_at: str, now: float | None = None) -> str:
    built = datetime.strptime(built_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    days = int(((now or time.time()) - built.timestamp()) // 86400)
    return "today" if days < 1 else f"{days} day{'s' if days != 1 else ''} ago"


def run(ctx, query, path_glob, max_results, response_format, source):
    from tools import ToolError

    problems = validate(ctx, {"query": query, "path_glob": path_glob, "max_results": max_results})
    if problems:                      # the boundary refuses these first; never answer one as "no matches"
        raise ToolError("Malformed search. " + " ".join(problems))
    repo_root = Path(ctx.repo_root)
    if source == "live":
        files, index_age = _disk(repo_root), None
    else:
        index = load_index(repo_root.parent.parent)
        files, index_age = index["files"], f"built {index['built_at']}, {age(index['built_at'])}"
    needle = query.strip().lower()
    width = EXCERPT[response_format]
    hits = []
    for rel, lines in sorted(files.items()):
        if not _matches(rel, path_glob):
            continue
        for number, line in enumerate(lines, 1):
            if needle in line.lower():
                hit = {"path": rel, "line": number, "text": line.strip()[:width]}
                if response_format == "detailed":
                    hit["before"] = lines[number - 2].strip()[:width] if number > 1 else ""
                    hit["after"] = lines[number].strip()[:width] if number < len(lines) else ""
                hits.append(hit)
    shown = hits[:max_results]
    out = {"query": query, "query_valid": True, "path_glob": path_glob, "source": source,
           "count": len(hits), "shown": len(shown), "truncated": len(hits) > len(shown),
           "index_age": index_age, "results": shown}
    if out["truncated"]:              # what was not shown, by file, so a cut result is still a map
        per: dict[str, int] = {}
        for hit in hits:
            per[hit["path"]] = per.get(hit["path"], 0) + 1
        out["by_file"] = [{"path": p, "count": n} for p, n in sorted(per.items(), key=lambda t: (-t[1], t[0]))[:BY_FILE]]
    return json.dumps(out)
