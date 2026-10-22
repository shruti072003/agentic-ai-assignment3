# ARCHITECTURE.md · RepoMind on the seven layers

For each layer: what does this job, and where is it enforced. Cite code as a file
path and line number joined by a colon, inside backticks; a checker opens every
reference (`checks/FORMATS.md`, section 8). A layer held only by a sentence in a
prompt (any file under `prompts/`) is **EMPTY**, and so is a layer nothing does
yet. Status is one of: present, partial (say what is missing), or **EMPTY**. Each
assignment brings this file up to date.

What each layer is for (Lecture 1, slides 42 to 44):

- **Model**: reasoning, choosing tools, generating text.
- **Harness**: runs the loop, builds each prompt, trims old history.
- **Tools**: where the system acts on the world.
- **Memory & State**: what survives a step, a session, a restart.
- **Control & Policy**: what the system may do, and when it must ask.
- **Observability**: a step-by-step record of what it did.
- **Evaluation**: measuring quality, before and after release.

The table below is RepoMind as Assignment 3 leaves it.

| Layer | What does this job | Where it is enforced | Status |
|---|---|---|---|
| Model | The configured model, called only through the metered client | `harness/client.py:120` | present |
| Harness | The loop, with three bounds checked in code; a plan validated before it runs, with exactly one PR step right before the one comment step, last; an executor that runs only the plan's steps and drafts the comment before either write | `agent/loop.py:38`, `ways/planned.py:97`, `ways/planned.py:174`, `ways/planned.py:321` | present |
| Tools | Five namespaced, strict, capped definitions behind one registry. Every call is validated at the boundary (schema, case-insensitive enums, each tool's content rules) before it runs, and every result against its declared shape; every write carries a key the harness derives from the run, the plan step and the canonical arguments, is checked before every attempt, and is read back after a timeout | `tools/__init__.py:238`, `tools/__init__.py:118`, `tools/__init__.py:339`, `tools/__init__.py:382` | present |
| Memory & State | A run's progress file, outside the model's context; the tracker, which keeps each write once per key | `ways/notes.py:68`, `harness/tracker.py:208` | partial: nothing survives a run |
| Control & Policy | A pre-call hook that fails closed; the third exit, which stops a run whose write cannot be confirmed and hands it to a person; the second pass, which holds a draft that still fails; a draft with no file opens no PR | `tools/__init__.py:275`, `tools/__init__.py:366`, `ways/planned.py:363`, `ways/planned.py:419` | present |
| Observability | One trace line per model call and per tool call, the plan as data before it runs, one call-record line per attempt of every tool call written by the registry, and protocol errors in the harness log | `agent/loop.py:50`, `ways/planned.py:93`, `tools/__init__.py:406`, `tools/__init__.py:223` | partial: records, no alerts |
| Evaluation | The grounded check reads the tracker, never the agent; Part D's counts come from the tracker's database | `eval/check_triage.py:6`, `eval/count_writes_03.py:59` | partial: offline only |

**Empty layers: 0 of 7**, and three are partial.
