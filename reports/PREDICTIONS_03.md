# Predictions • Assignment 3, Part D

Each arm has 12 runs (six evaluation issues, two runs each), all with `FLAKY_WRITES=1`.

### Duplicate comments

- **My harness:** I expect no duplicate comments. Each retry uses the same key, and the harness checks the tracker after a timeout, so it finds a comment that is already there.
- **Control:** I expect duplicate comments in about 5 of the 12 runs, because each attempt sends a new key and does not read the tracker first, so a comment that timed out after the write is written again.

### Duplicate PRs

- **My harness:** I expect no duplicate PRs. The check before each attempt finds the open PR from the branch of the issue, and the key stops a second write.
- **Control:** I also expect no duplicate PRs. The control still does the check before each attempt, and the branch name `repomind/issue-<n>` identifies the PR, so a retry finds it.

### The tool that I expect the model to use incorrectly most

- **`code_search`.** In the development runs, the model searched using the full issue title, but that text did not appear in the code, so the search returned no matching lines.