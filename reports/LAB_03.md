# Assignment 3 · A tool surface that tells the truth, and writes that happen once

The model is `gpt-4.1-mini`, accessed through the OpenAI API (`provider: openai-compatible`, with no `base_url`). I used the same model for Assignment 2. The input token price is $0.40 per million uncached tokens, from `pricing.yaml` (https://developers.openai.com/api/docs/pricing, read on 2026-10-21).

All numbers in this report match `reports/LAB_03.json`. For write counts, I used `eval/count_writes_03.py`, which reads `tracker/tracker.db` directly rather than relying on the traces.

## Part A · The five definitions

Running `python tools/audit_schemas.py` reports **5 strict tools, 0 optional parameters, and 2 nullable parameters**: `code_search.path_glob` and `code_read_file.start_line`.

**Strict mode.** OpenAI has a strict mode, so I set `"strict": true` on every tool. The third `make ping` call sent all five definitions together, as `registry.schemas` sends them, and OpenAI accepted them as strict. The response is recorded in `runs/_ping.jsonl`, so `compiles` is `true`.

**Consolidation.** No task always uses the same calls in the same order: the read depends on the search result, a search can be revised and rerun, and the PR is skipped when the file is `none`.

**`search_repo` before and after.** The starter version is from commit `25af70e`, and my version is in `tools/search_repo.json`.

| Before | After |
|---|---|
| `search_repo` | `code_search` |
| Description: "Search the code." | Searches for literal, case-insensitive text within individual lines of text files, excluding `config/`. It does not search filenames. Returns `query`, `query_valid`, `path_glob`, `source`, `count`, `shown`, `truncated`, `index_age` and `results`, with `by_file` when more lines match than are shown. |
| `query`: string, "Text to look for" | Required string of 1–200 characters. Shorter queries usually match more results. Multiple words must appear together and in order. Regular expressions are not supported. |
| `max_results`: integer, "How many results. Default 10." | Required integer from 1–20, usually 10. Each hit costs about 30 tokens, and `count` reports the total number of matches. |
| `filter`: optional string | `path_glob`: required string or null. Null searches all eligible files, while a glob such as `src/api/**` narrows the search. The description lists the allowed characters and prohibits a leading `/` or `..`. |
| `deep`: boolean, default false | `source`: required enum, `index` or `live`. The index is faster but may be outdated; live search is slower but uses current files. |
| No response format | `response_format`: required enum, `concise` or `detailed`. Concise results show 120 characters per hit. Detailed results show 240 characters and surrounding lines, using roughly three times as many tokens. |
| Not strict, no `additionalProperties`, 3 optional parameters | `strict: true`, `additionalProperties: false`, 0 optional parameters, and 1 nullable parameter. |
| "No matches." for both an empty result and a malformed query | An empty result returns `count: 0` and `query_valid: true`. A malformed query returns an error identifying the problem. |

## Part B · Errors

| Tool | Transient | Permanent | Ambiguous |
|---|---|---|---|
| `issues_read` | The filesystem or database temporarily refuses a read. The registry retries up to three times, then tells the model to retry after 30 seconds. | The issue does not exist, the number is less than 1, or an argument is missing, unknown, or has the wrong type. The boundary rejects invalid arguments. | None, since reads do not change anything. |
| `code_search` | The index read is temporarily refused, so the registry retries. | The query is empty or longer than 200 characters, the glob is invalid, `max_results` is outside 1–20, or `response_format` or `source` is not an allowed enum value. | None. |
| `code_read_file` | The filesystem temporarily refuses a read, so the registry retries. | The path is not an exact repository file, is under `config/`, escapes the repository, has a `start_line` past the end of the file, or refers to a non-text file. The error lists nearby paths when possible. | None. |
| `issues_post_comment` | The write fails before reaching the tracker. The mock tracker does not simulate this failure; its timeouts can occur after the write has committed. | The issue does not exist, the body is empty or longer than 4,000 characters, the body does not start with `label:` and `file:`, or a hook rejects the call. | A `TrackerTimeout` occurs after the write has committed. The registry reads back by key and retries with the same key only if the comment is not found. If the tracker cannot be read, the registry escalates. |
| `pulls_open_draft` | The same as for comments. | The issue does not exist, the branch does not match `repomind/issue-<n>`, the title is empty, too long, or contains multiple lines, or the body is empty or too long. | The same as for comments, but the read-back checks both the key and the branch. |

**Protocol errors: `generic_result`.** A protocol error is an unknown tool, or arguments that are not a JSON object. The registry writes the full error to `runs/<run_id>.harness.log`. The model gets only this text (152 characters, no tool names):

*"Error: that call could not be run, and no change to its arguments will fix it. Use only the tools you were offered, with a JSON object of arguments."*

I selected this policy for three reasons:

1. All model APIs must get an answer to each tool call before the next request.
2. If one incorrect tool name stops the run, the task is lost. Often the model corrects the name on the next turn.
3. The generic result gives the model nothing to argue with. The three bounds of the loop limit the cost if the model tries again and again.

**The `read_file` near-miss error** for `src/report.ts`. The tool compares the path with the file list of the repository. It does not ask the case-insensitive disk. This is the exact text:

`Error: No such file: src/report.ts (paths are case-sensitive). Nearest existing paths: src/Report.ts, src/report.tsx, src/reports.ts. Retry with one of them exactly; this path will not succeed.`

## Part C · Writes that may already have happened

### The key formula

The harness generates the key in `derive_key` in `tools/__init__.py`. The model never sees or sets it.

```python
sha256(run_id + "|" + json(plan_step) + "|" + canonical(args))[:32]
```

`canonical` serializes JSON with sorted keys and no extra whitespace. The arguments are taken after boundary validation, without the key itself. Since retries in `registry.call` reuse the same `args` object, every attempt gets the same key.

### How a run whose file is `none` skips the PR

The executor handles this in `open_pr` in `ways/planned.py`. It drafts the comment first and checks it against the comment tool's boundary and the repository's file list. If the draft's `file:` line is `none`, the executor records the PR step as `skipped` and adds a `pr_skipped` trace line. The comment is still posted. In the measured runs, 5 of the 12 harness comments named `none`, and none of those five runs opened a PR.

### One call-record line for a write

In run `planned-4201-s02-7c2c84`, the PR write committed but timed out. The first line records the timeout; the second, with the same key, is the read-back that found `pr-3` already open, so the registry did not retry. I've shortened the body.

```json
{"run_id": "planned-4201-s02-7c2c84", "t": 7.209, "tool": "pulls_open_draft", "role": "open_pr", "args": {"number": 4201, "branch": "repomind/issue-4201", "title": "Use environment variable for base URL in password reset email", "body": "File: src/notifications/templates.ts\n\nThe password reset email link currently uses a BASE_URL constant … "}, "plan_step": 4, "key": "725f2ef2fc0cba5d23f04fb1e7d44ca4", "attempt": 1, "outcome": "timeout", "is_error": true, "result": "timeout: the tracker did not answer in time", "latency_s": 0.003, "cost_usd": 0.0}

{"run_id": "planned-4201-s02-7c2c84", "t": 7.21, "tool": "pulls_open_draft", "role": "open_pr", "args": {"…": "the same"}, "plan_step": 4, "key": "725f2ef2fc0cba5d23f04fb1e7d44ca4", "attempt": 1, "outcome": "ok", "is_error": false, "result": "read back after the timeout: {\"pr\": \"pr-3\", \"issue\": \"issue-4201\", \"branch\": \"repomind/issue-4201\", \"status\": \"opened\"}", "latency_s": 0.004, "cost_usd": 0.0}
```

## Part D · Measure

### 1. The cost of the tool definitions

I used `eval/surface_03.py count` to measure the token cost of the definitions. It sends the same one-message request with and without a definition and calculates the difference in `prompt_tokens`. The results are saved in `eval/surface_counts.json`.

For the starter version, I used the `tools.SENT` fields from the initial starter code (commit `25af70e`) to match what the original registry sent. For my version, I measured the definitions as `registry.schemas` actually sends them. The individual tool counts include OpenAI's tool-use preamble, which is included once in each call that carries tools.

| Prompt tokens | Starter | Mine | Increase |
|---|---:|---:|---:|
| All five tools | 195 | 2,551 | 13.1× |
| `read_issue` / `issues_read` | 41 | 302 | 7.4× |
| `search_repo` / `code_search` | 75 | 808 | 10.8× |
| `read_file` / `code_read_file` | 38 | 510 | 13.4× |
| `post_comment` / `issues_post_comment` | 48 | 451 | 9.4× |
| `open_pr` / `pulls_open_draft` | 57 | 544 | 9.5× |
| Planner's tool list (`_tool_list`) | 106 | 1,707 | 16.1× |

The 12 harness runs made 86 model calls, or 7.17 calls per run. There were 12 plan calls and 12 revise calls, each carrying `submit_plan` and the tool list. Since `submit_plan` did not change, I left it out of the comparison. The other 62 calls were fill calls, each carrying only the tool needed for that step: 27 `code_search`, 16 `code_read_file`, 12 `issues_post_comment` calls for the draft, and 7 `pulls_open_draft` calls.

Using `eval/surface_03.py price` and the rate of $0.40 per million uncached tokens gives these costs:

- **Starter definitions:** 24 × 106 + (27 × 75 + 16 × 38 + 12 × 48 + 7 × 57) = 2,544 + 3,608 = 6,152 tokens. This is 512.7 tokens per run, or **$0.000205 per run**.
- **My definitions:** 24 × 1,707 + (27 × 808 + 16 × 510 + 12 × 451 + 7 × 544) = 40,968 + 39,196 = 80,164 tokens. This is 6,680.3 tokens per run, or **$0.002672 per run**.
- **All five definitions on every call:** 86 × 2,551 = 219,386 tokens. This is 18,282.2 tokens per run, or **$0.007313 per run**.

My definitions cost about 13 times as much as the starter definitions, adding roughly $0.0025 per run. They account for 6,680 of the 12,565 prompt tokens in an average run, which is a little over half of what the model reads.

The planned executor helps keep this cost down because each call only carries the definition for its current step. For example, a search call carries 808 tokens of definitions rather than 2,551 for all five. Sending all five definitions on every call would cost about 2.7 times as much: $0.0073 instead of $0.0027 per run. Caching could lower the price, but it would not reduce the space the definitions occupy in the context.

The extra detail did help in a few ways:

- **Malformed searches were easier to identify.** The boundary rejected five calls before they ran, and each error named the field that needed fixing.
- **The model could recognize empty results.** Of 53 searches, 29 returned no matches. Usually, the model tried a shorter query next, such as "Monday summary email" → "summary email" → "summary". However, two runs repeated queries such as "TSV" or "Malformed row" after getting zero results.
- **Incorrect paths produced useful errors.** Eleven reads used invented paths. Each error returned nearby valid paths or listed the files in the folder, and no read silently returned a different file.

### 2. What the tracker recorded

I ran `python -m eval.count_writes_03`, which reads `tracker/tracker.db` and groups writes by `run_id`. Both arms had 12 runs, covering the six evaluation issues twice each, with `FLAKY_WRITES=1`.

| Measure | My harness | Control |
|---|---|---|
| Triage comments per run (expected: 1) | Exactly 1 in every run | Mean 1.67, maximum 3; 6 of 12 runs had duplicates (four with 2 comments and two with 3) |
| Open PRs from `repomind/issue-<n>` (expected: 1 unless `file: none`) | 7 runs with 1 and 5 with 0; **12/12 correct** | 9 runs with 1 and 3 with 0; **12/12 correct** |
| Writes that committed and then timed out | 10 | 18 |
| Runs that ended as `escalated` | 0/12 | 3/12 |
| Correct triage according to the grounded check | 3/12 | 1/12 |

The control's lower triage score is partly due to duplicate comments: the grounded check expects exactly one comment, so a run that posts the correct answer twice still fails. Triage success fell from 8 of 18 planned runs in Assignment 2 to 3 of 12 here; with two runs per issue, I cannot tell how much of that is random variation. Section 3 shows the larger problem: in 20 of the 24 runs, the model never saw the answer file.

### 3. Four ways the model misused tools

#### Right tool, wrong arguments

The boundary checks helped, but they did not solve every problem.

- The boundary rejected five calls before execution, with each error identifying the relevant field. For example: `source: 'docs' is not allowed; use one of index, live`.
- Four of the five came from `source` values the reviser wrote into the plan's `args_json`. Strict mode controls the structure of the compiled tool call, not that string, so the boundary still has an important role. The fifth was a search sent with no arguments at all.
- The repair turn cannot change a value that the plan has already fixed. The two searches with a plan-fixed `source` were each refused a second time after their repair turn.
- Eleven reads used invented paths, including `src/email/SummaryEmail.ts` and `src/export/ExportReport.ts`. Each error returned nearby paths or a folder listing. By comparison, on this case-insensitive filesystem, the starter could open `src/Report.ts` when given `src/report.ts` and report success.
- Even valid arguments can be unhelpful. Twenty-nine of 53 searches found nothing. Some used the entire issue title as a phrase, while others used `path_glob` values for languages that the repository does not contain. The schema cannot determine whether a valid query will find the information the model needs.

#### Wrong tool, valid call

There were no `off_plan` lines across the 24 runs. Each fill call received only the tool assigned to that step, which prevented the model from choosing an unrelated tool.

The prefixes also help the planner distinguish tracker tools from code tools. However, most of this protection comes from the executor built for Assignment 2. My changes mainly make `pulls_open_draft` and `issues_post_comment` easier to tell apart.

#### Correct call, misread result

No run treated `count: 0` as a search hit, and no run named a file that was absent from the search results. However, two examples show that the model can still misinterpret the information it receives.

- **Issue 4203:** Searching for "Malformed row" returned the message key `errors.rowShape` in `src/i18n/en.json`. None of the runs searched for that key, which would have helped it reach the relevant code. Three runs read the translation file, and one did not read any file. All four posted either `file: none` or `src/i18n/en.json`.
- **Issue 4206:** Searching for "currency" matched 103 lines, but only 10 were returned. All four runs read `src/export/csv.ts` and named it in their comments, even though the result was truncated. The correct file was `src/ingest/dedupe.ts`. The result included `truncated: true`, but the model did not narrow the search before choosing a file.

#### No call at all

This problem remained, and it was the hardest to identify from the execution logs.

In all four runs for issue 4204, three searches returned nothing and two file reads used nonexistent paths. The model never searched for the date logic in `src/util/dates.ts`, which would have helped identify the cause. Instead, it posted `label: question, file: none`.

The execution logs did not show an obvious failure. Every call either succeeded or returned a valid error, all four runs ended as `completed`, and the tracker contained one comment and the correct number of PRs (zero).

I could identify the problem only because the grounded check compared the comments against the known answers in `fixtures/answers.yaml`. Across the 12 harness runs, the writes were correct in every run, but 9 runs still had incorrect triage.

A call that never happens leaves no trace line. Without known answers, the traces, call records, and tracker would not reveal that the model had failed to find the answer. Detecting this kind of problem requires tasks with known answers, which is the focus of Assignment 7. Better tool definitions alone cannot fix it.

### 4. Predictions that missed

I wrote and pushed `reports/PREDICTIONS_03.md` before the first evaluation run. Four of the five predictions were correct: no duplicate comments in my harness, no duplicate PRs in either arm, and `code_search` as the most misused tool.

One prediction missed. I predicted duplicate comments in about 5 of the 12 control runs, and there were 6. The reasoning was still consistent with what happened: if a comment commits and then times out, a retry with a new key and no read-back creates a duplicate. The number of affected runs depends on which attempts time out. The control had 18 writes that committed before timing out, compared with 10 in my harness, and two control runs had three timeouts in a row.

## AI assistance

I used Claude Code during this assignment to help implement and refine parts of the tool registry, fix issues in the agent loop, and refine the prompts. I examined the experimental outputs to understand the results and write the report.

For the written work, I wrote the initial drafts of the report sections myself. I then used Claude Code to review them for grammatical issues, inconsistencies, and clarity. I reviewed its feedback and made the final decisions about the predictions, interpretation of the results, and presentation of the findings.

I used Claude Code to help analyze the experimental results and verify the reported numbers against the run outputs and tracker. It also helped identify issues and inconsistencies in the report, which I reviewed and corrected based on the available evidence. The implementation and results were checked using the project's tests and validation scripts.

Finally, I reviewed the report against the assignment requirements and checked that the conclusions were supported by the experimental results. Claude Code served as an aid during implementation, analysis, and editing, as well as a second-pass checker, while I made the final decisions about the report's content and presentation.