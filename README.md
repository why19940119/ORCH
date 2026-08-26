# ORCH — Human-Gated AI Task Orchestrator

ORCH is a local Python task orchestrator built around explicit
dispatch controls:

- Task dependencies
- Retry and timeout handling
- Structured policy checks
- Immutable artifacts
- Scoped snapshots and stale-state detection
- Human approval gates
- Advisory-only AI integration
- Git-backed change history

ORCH does **not** allow an AI model to execute commands directly.
AI can produce an advisory only. Command execution remains controlled
by policy, freshness / snapshot validation, and explicit human approval.

## Current Status

Current branch:

```text
main
```

Current implementation includes:

```text
v0.1–v0.8   Task queue, dependencies, approval, freshness foundations
v0.9        Generic policy engine
v0.10       Structured artifact policies
v0.11       Deterministic mock AI advisory
v0.12       Immutable artifacts, scoped snapshots, bounded review
v0.13       OpenRouter Mistral advisory adapter and preflight binding
v0.14       Advisory-gated dispatch and lifecycle-safe snapshots
v0.14b      --advisory-preflight CLI opt-in
v0.15       Documentation and consolidation
```

## Core Architecture

```text
Human Operator
      |
      | add-task / approve / status / run queue
      v
+-------------------------------------------------------+
| mini_orch.py                                          |
|                                                       |
| dependency check                                      |
| → required policy evaluation                          |
| → optional AI advisory preflight                      |
| → explicit human approval                             |
| → subprocess.run()                                    |
+-------------------------------------------------------+
      |                    |                    |
      v                    v                    v
task_queue.json       state/               Python workers
                      task_status.json     domain task logic
                      events.jsonl              |
                                                  v
                                            output/*.json
                                                  |
                     +----------------------------+
                     |
                     v
+-------------------------------------------------------+
| artifact_store.py                                     |
| immutable object → manifest → latest pointer          |
+-------------------------------------------------------+
                     |
                     v
+-------------------------------------------------------+
| snapshot_store.py                                     |
| scoped task / artifact / policy fingerprints          |
| stale-state validation                                |
+-------------------------------------------------------+
                     |
                     v
              Git commits / GitHub
```

## Dispatch Rules

A task can run only after all applicable gates pass:

```text
dependency completed
→ required policies allowed
→ advisory preflight valid, when enabled
→ human approval, when required
→ subprocess dispatch
```

Any failure blocks dispatch:

```text
dependency incomplete
policy failure
missing artifact
invalid JSON field
stale scoped snapshot
stale advisory preflight
AI schema failure
AI provider failure
human_review_required
approval missing
```

## Task Lifecycle

```text
todo
→ running
→ done

todo
→ waiting_approval
→ approved
→ running
→ done

todo
→ blocked

running
→ retrying
→ running
→ failed
```

## Quick Start

Run the queue:

```bash
python3 mini_orch.py
```

Show task status:

```bash
python3 mini_orch.py status
```

Add a basic task:

```bash
python3 mini_orch.py add-task \
  task_example_001 \
  "Write a local output file" \
  "python3 worker_example.py" \
  100
```

Add a task requiring human approval:

```bash
python3 mini_orch.py add-task \
  task_example_002 \
  "Run an approved local task" \
  "python3 worker_example.py" \
  101 \
  --approval
```

Approve a waiting task:

```bash
python3 mini_orch.py approve task_example_002
```

Run the queue again:

```bash
python3 mini_orch.py
```

## Dependencies

Add a task with dependencies:

```bash
python3 mini_orch.py add-task \
  task_example_003 \
  "Run after prerequisite task" \
  "python3 worker_example.py" \
  102 \
  --depends-on task_example_001
```

A task remains blocked until every dependency has status `done`.

## Structured Policies

Require a local artifact:

```bash
python3 mini_orch.py add-task \
  task_example_004 \
  "Require an output artifact" \
  "python3 worker_example.py" \
  103 \
  --require-artifact output/example.json
```

Require a JSON field value:

```bash
python3 mini_orch.py add-task \
  task_example_005 \
  "Require a successful JSON result" \
  "python3 worker_example.py" \
  104 \
  --require-json-field output/example.json status '"success"'
```

Current policy evaluators:

```text
artifact-exists
json-field-equals
```

## Advisory Preflight

An advisory task is explicit opt-in.

```bash
python3 mini_orch.py add-task \
  task_example_006 \
  "Run a human-approved AI-advised task" \
  "python3 worker_example.py" \
  105 \
  --advisory-preflight
```

`--advisory-preflight` automatically enables:

```text
requires_approval = true
```

The advisory flow is:

```text
task definition
→ scoped snapshot
→ OpenRouter advisory request
→ local JSON schema validation
→ immutable advisory artifact
→ waiting_approval
→ human approval
→ snapshot revalidation
→ command dispatch
```

The same valid advisory is reused after approval. ORCH does not make a
second AI request merely because a task moved from `waiting_approval`
to `approved`.

## OpenRouter Configuration

Do not commit API keys.

```bash
export OPENROUTER_API_KEY='your-key'
export OPENROUTER_MODEL='mistralai/mistral-medium-3.1'
```

Check configuration without printing the secret:

```bash
python3 - <<'PY'
import os

print(
    "OPENROUTER_API_KEY:",
    "configured"
    if os.getenv("OPENROUTER_API_KEY")
    else "missing",
)

print(
    "OPENROUTER_MODEL:",
    os.getenv(
        "OPENROUTER_MODEL",
        "mistralai/mistral-medium-3.1",
    ),
)
PY
```

AI output must satisfy this local schema:

```json
{
  "summary": "string",
  "risks": ["string"],
  "recommended_action": "advisory_only | request_human_review | no_action",
  "confidence": 0.0,
  "execution_authority": "none"
}
```

AI has no command execution authority.

## Testing

Compile key modules:

```bash
python3 -m py_compile \
  mini_orch.py \
  snapshot_store.py \
  advisory_dispatch.py \
  advisory_preflight.py \
  openrouter_advisory.py
```

Run deterministic tests:

```bash
python3 -m unittest discover \
  -s tests \
  -p 'test_*.py' \
  -v
```

Current verified coverage includes:

```text
No advisory configuration → no AI call
No human approval requirement → advisory dispatch blocked
Valid advisory preflight → dispatch allowed
Stale snapshot → dispatch blocked
Existing blocked preflight → no automatic AI retry
Root lifecycle transitions → no false snapshot stale result
Semantic root state change → snapshot stale
Dependency state change → snapshot stale
```

## Repository Layout

```text
mini_orch.py                 Generic queue runner and CLI
task_queue.json              Task definitions
state/                       Runtime status and event log
output/                      Worker output files
artifact_store.py            Immutable artifact publication
snapshot_store.py            Scoped snapshot construction and validation
openrouter_advisory.py       OpenRouter advisory-only adapter
advisory_preflight.py        Task-bound advisory validation
advisory_dispatch.py         Advisory dispatch gate
review_state_machine.py      Bounded review lifecycle model
tests/                       Deterministic unit tests
.env.example                 Environment variable template
```

## Safety Boundaries

```text
AI execution authority: none
External provider key: environment variable only
Advisory task: explicit opt-in only
Advisory task: human approval required
Stale snapshot: blocks dispatch
Policy failure: blocks dispatch
Provider/schema failure: blocks dispatch
No automatic AI retry after blocked preflight
No force push required for normal Git workflow
```

## Current Constraints

ORCH is currently a local prototype.

```text
Single user
Single local worker process
JSON-file runtime state
No database
No RBAC
No secret manager / key rotation
No web UI
No distributed workers
No production observability stack
```

## Next Direction

The next work should prioritize consolidation rather than more features:

```text
1. Move historical regression demos out of the normal queue over time.
2. Keep deterministic tests under tests/.
3. Add cost and call budgets before broader AI usage.
4. Add a durable secret-management strategy.
5. Add richer operator status views and audit reporting.
```

## ORCH Chat

ORCH includes a local browser chat panel:

```bash
python orch_ui.py
```

Open:

```text
http://127.0.0.1:5050/chat
```

Available modes:

```text
General Chat
→ General OpenRouter Mistral conversation

ORCH Context Chat
→ Read-only task, policy, advisory, artifact metadata,
  snapshot summary, and latest-event context
```

Chat safety boundary:

```text
execution_authority = none
no approve action
no queue run action
no retry action
no task creation
no policy modification
no artifact deletion
no connector write action
no API key exposure
```

Each click on `Ask ORCH Chat` creates at most one OpenRouter request.
Chat history exists only in the running local Flask process and is
cleared when the UI server stops.
