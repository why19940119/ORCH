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
v0.16       Local Operator Console (dashboard, tasks, events, artifacts, chat, i18n)
v0.17       Chat attachments engine (chat_attachments + orch_chat vision)
v0.18       Cross-border e-commerce demo (7 modules, Approval Inbox + Audit Log),
            chat upload wiring
v0.18.1     zh-Hant/zh-Hans module names (銷售中心 … 審計紀錄), demo sample
            data in ORCH Context chat, chat replies follow the UI locale
v0.18.2     Review fixes: safe PDF/magic-byte upload validation, 16MB request
            cap (413), batch-then-write uploads + retention sweep, demo queue
            in gitignored state/ecom_demo_queue.json, locked run_queue status
            writes, CLI refuses demo approvals, audit after the gate,
            localised upload/chat errors and draft titles, pytest config
v0.19.0     Store-data CSV import (products / orders / traffic): /import page +
            commerce_import.py CLI, per-row validation report, 真實匯入數據
            banner, real metrics in Market Dashboard / Campaign Engine, AI
            suggestion drafts via the Approval Inbox, Content Studio uses
            imported products
v0.19.1     CSV import review fixes: compact sized metrics block for real-AI
            drafts (up to 3,000 chars, CSV text sanitised and quoted as data),
            products-only upload keeps stored orders (unmatched ones are
            excluded from metrics with a warning), skipped rows counted,
            trailing empty header cells ignored, same order_id + sku rows
            merged, Big5 (cp950 / big5hkscs) fallback, reset confirmation
v0.20.0     Accounts and governance: login (local accounts, admin / editor /
            approver roles), no self-approval, per-module approvers, approval
            deadlines + overdue escalation, second-admin approval of account
            changes, retention / purge, 權限清單 (permissions list) + CSV export;
            ORCH Context chat answers from imported store data (as-of date)
v0.20.1     Chat accuracy: precomputed last 7 / 30 day totals with previous
            period + change, best / worst days, top pages, complete weekly
            series (partial ISO weeks flagged with their dates), HK$ money,
            「匯入數據」 label, no-estimation rule, Cantonese register;
            internal labels filtered from replies; sample won leads unranked;
            stale account-change requests refused / auto-closed (audited);
            review fixes: filter only known internal names (UI-locale words),
            partial / uncovered periods flagged (no change vs no data),
            requests of demoted admins auto-closed, stacked phone composer,
            colloquial Cantonese + 營業額, audit / import version v0.20.1
v0.21.0     SQLite state + Docker: all mutable state in state/orch.db (WAL,
            one-transaction approve + audit, automatic one-time migration from
            the JSON files with a timestamped backup), Dockerfile +
            docker-compose.yml (non-root, /healthz, persisted session key),
            /setup first-admin wizard, branding (client name, logo, target
            market), version in the footer, backup / restore / smoke scripts,
            reverse-proxy (HTTPS) option, zh-Hant docs in docs/;
            phones (<=720px): compact top bar (ORCH, current page, ☰ 選單)
            with nav / language / 登出 in a collapsible menu that works
            without JavaScript (desktop unchanged)
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

## Operator Console (quickstart)

Local read-only Operator Console (dashboard, tasks, events,
artifacts, chat):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env once: set ORCH_UI_SECRET_KEY and optional OPENROUTER_API_KEY.
# Already-exported shell variables still win over .env.

python orch_ui.py
```

Open:

```text
http://127.0.0.1:5050
```

Chat lives at `http://127.0.0.1:5050/chat`.

## Local .env

Copy `.env.example` to `.env` and fill placeholders once. On startup,
`orch_ui.py` loads `.env` for any keys not already set in the process
environment. `.env` is gitignored. Never commit real keys; ORCH never
prints secret values.


Durable browser sessions need a stable Flask secret. Set
`ORCH_UI_SECRET_KEY` in the environment (see `.env.example`).
If unset, ORCH uses an ephemeral per-process secret and sessions
reset on restart. Session cookies use HttpOnly + SameSite=Lax
(Secure stays off for local HTTP).

### Acceptance checklist

```text
[ ] Dashboard / Tasks / Events / Artifacts / Chat routes load
[ ] Status badges render distinct classes on /tasks
[ ] Chat desktop composer stays sticky and opaque
[ ] Enter sends; Shift+Enter inserts a newline
[ ] Narrow (~400px) composer is single-column and usable
[ ] Missing/invalid CSRF on POST /chat returns 400
[ ] Artifact detail rejects manifest paths outside artifacts/
```

## ORCH Chat

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

Chat attachments (v0.18.0 wiring): the paperclip button attaches up to
3 files (PDF, TXT, MD, PNG, JPG, WEBP, GIF). Text or a file is enough.
Files are validated and contained under `uploads/chat/` (gitignored) by
`chat_attachments.process_uploaded_files`; session history and the chat
audit artifact keep metadata only (name, kind, mime, size, sha256),
never bytes or stored paths. A rejected file is shown as a normal user
error.

Upload hardening (v0.18.2): request bodies are capped at 16MB
(`MAX_CONTENT_LENGTH`; larger requests get a localised 413). Each file's
magic bytes must match its extension (PDF, PNG, JPG, WEBP, GIF; TXT/MD
must be UTF-8 text). The whole batch is validated and PDFs are parsed
(first 50 pages) in memory before anything is written; any failure
removes the batch directory. Upload batches older than 24h are swept on
the next upload. Upload, attachment and chat errors are shown through
`ui_i18n` in the UI language (`err_att_*`, `err_chatcode_*`).

ORCH Context chat and the e-commerce demo (v0.18.1): `build_chat_context`
adds an `ecommerce_demo` block from `commerce_demo.chat_context`. It is a
compact, read-only slice of `demo/sample_data.json`: SKUs, inquiries,
order leads and Knowledge Base entries whose IDs (for example
`SAMPLE-001`, `INQ-S-002`) or names/keywords appear in the question,
plus a one-line-per-SKU catalog summary. No secrets, no state writes,
no execution authority. The UI locale is passed to `ask_orch(locale=...)`
so answers, refusals and guidance come back in the user's language
(zh-Hant by default).

### Chat uses imported data (v0.20.0)

The block is now `store_data` from `commerce_demo.store_chat_context`:

```text
import active   the store's imported products / orders / traffic: the same
                compact summary as the AI insight drafts (top SKUs by revenue
                and by units, latest-day ranking, last 7 order dates, recent
                weeks, low stock / days of cover, traffic by source,
                conversion, excluded order lines) + matching products by SKU
                or name; labelled as imported data with as_of_date = latest
                order date. KB / inquiries / leads stay sample (labelled).
import off      the sample data, labelled SAMPLE, with a sales ranking note:
                the sample has no per-product sales, so only won order leads
                and weekly order counts are listed (no invented numbers).
```

For "today / this week / this month" questions the model is told to answer
from the latest date in the data and say so (「數據截至 2026-09-25」), and
never to claim real-time data. The context label is neutral
(`REFERENCE_DATA`) and the model is told not to mention internal labels,
keys or file names. The Chat page shows which data ORCH Context uses.

### Chat accuracy (v0.20.1)

A real-API test (12 questions) found one fabricated 30-day figure and a few
partial answers, so the imported-data summary (`commerce_demo.chat_summary`)
now gives the model every figure it needs, precomputed, in HK$:

```text
數據來源 / data source: 匯入數據 (imported data), as of 2026-09-28
last_7_days / previous_7_days / change_7_days     revenue, orders, units, abs + %
last_30_days / previous_30_days / change_30_days  (never dropped)
all_time_totals, days_with_orders, best / worst days by revenue
latest-day ranking, top products by revenue and by units
weekly_series   every ISO week, e.g. 2026-W40 (2026-09-28 to 2026-09-28,
                PARTIAL: 1 of 7 days)
recent days, low stock, traffic by source, top pages, conversion
```

The summary has a 6,000-character budget. When a store is large, lists shrink
level by level (top N, weeks, days) and say "x of y"; the period totals are
always kept. The prompt says: quote only given figures, never estimate,
extrapolate or do arithmetic, say when a metric is not in the data, always
use HK$, call the data 匯入數據 (zh-Hant) / 导入数据 (zh-Hans), never 進口,
and reply in Cantonese when asked in Cantonese. Internal labels
(`REFERENCE_DATA`, `store_data`, `data_source`, `*.json` file names) are
replaced with plain words in the UI language in ORCH Context replies before
they are shown or stored (`orch_chat.sanitize_reply`). Only the known internal
names are touched: other `*.json` paths, URLs and ordinary words such as
"summary" are left alone. A 7 / 30 day window that the data covers only
partly is marked PARTIAL (covered days of the window); a previous window with
no data is marked NOT COVERED and no change is given unless both windows are
fully covered. Account-change requests that no longer
apply (a second disable, the current role, a disabled or deleted target) are
refused when requested and auto-closed on approval or after another change
applies, with an `account_change_auto_closed` audit record.

## Cross-border e-commerce demo

A demo prototype of the Advolution ORCH AI cross-border e-commerce plan
(BUD 「申請易」 scheme proposal). It adds seven client-facing modules to
the Operator Console. **All data is SAMPLE data** (`demo/sample_data.json`:
a fictional brand, 30 `SAMPLE-xxx` SKUs, 16 inquiries, 10 order leads,
synthetic round-number KPIs). Every page shows a 示範數據 / SAMPLE banner.

Positioning (enforced in code, not just copy):

```text
ORCH is not a chatbot or a CRM.
AI only drafts, classifies, ranks and suggests.
Every outward item (content, prices, promotions, refunds, product claims,
customer-service replies) is a pending ORCH task until a NAMED human
approves the exact version in the Approval Inbox.
"Publish" is simulated: approval only records the channel in the audit log.
```

### Setup and start

```bash
source .venv/bin/activate
pip install -r requirements.txt
python orch_ui.py            # http://127.0.0.1:5050
```

Drafts use the existing OpenRouter path (`orch_chat.ask_orch`, one call
per click) when `OPENROUTER_API_KEY` is set, otherwise a deterministic
mock. Force the mock for a zero-cost demo:

```bash
ORCH_DEMO_FORCE_MOCK=1 python orch_ui.py
```

If the provider fails, the draft falls back to the mock and the
fallback reason is stored in the draft provenance (no retry).

### Pages to click (nav → "跨境電商示範 / E-commerce demo")

```text
/sales      ORCH Sales Hub        products, inquiries, order leads → draft next sales step
/content    ORCH Content Studio   draft product page / FAQ / ad copy for a SKU
/knowledge  ORCH Knowledge Base   approved specs, logistics, return/exchange, payment;
                                  propose a KB change (needs approval)
/leads      ORCH Lead Desk (查詢／線索台) inquiry classification + lead score → draft reply
/campaigns  ORCH Campaign Engine (推廣活動引擎) audience / creatives / A/B draft
/market     ORCH Market Dashboard traffic / inquiry / lead / order KPIs → draft insight
/inbox      Approval Inbox        pending drafts, risk tags, edit → new version,
                                  approve (channel) / reject (reason)
/audit      Audit Log             decision records + demo events
/import     Data import (數據匯入)   CSV import of real store data (v0.19.0)
```

### Approve flow

1. Sign in as an **editor** (v0.20.0; the operator is your account, not a
   typed name), pick an approval deadline and click
   **產生 AI 草稿 / Generate AI draft**.
2. The draft becomes an ORCH task `task_ecom_<kind>_<id>` in the
   gitignored demo queue `state/ecom_demo_queue.json` (v0.18.2; the
   tracked `task_queue.json` is never written — `mini_orch`, the Tasks
   page and the dashboard merge both queues) with
   `requires_approval: true` and an
   `artifact-exists` policy, and `waiting_approval` in
   `state/task_status.json`. Its text is an immutable artifact
   `ecom_draft_<id>` (edits publish a new version whose
   `parent_artifact_id` is the previous one).
3. In **Approval Inbox**, review the text and risk tags (price / refund /
   product claim / outward message, from `approval_inbox.py`), optionally
   edit and save a new version (editors), then approve with a publish
   channel or reject with a reason (approvers; never your own draft). Approval is locked to the
   version shown: approving a stale version is refused.
4. The decision goes through `mini_orch.decide_approval` (the same gate
   `mini_orch.py approve` uses), so the Tasks page and dashboard show it.
   The `ecom_audit` record is published only after that gate succeeds.
   `python3 mini_orch.py approve task_ecom_…` is refused with a pointer
   to `/inbox`, because only the inbox writes the audit record.
   `mini_orch.py` status writes use the same file lock as the UI
   (`state/.ecom_demo.lock`).
   Running `python3 mini_orch.py` afterwards dispatches the approved task
   to `worker_ecom_publish_record.py`, which only verifies the audit
   record (no external call). Rejected tasks are never dispatched.

### Where the audit lands

```text
artifacts/manifests/artifact_ecom_audit_*.json   immutable decision record
artifacts/latest/ecom_audit.json                 latest pointer
state/events.jsonl                               ecom_draft_created, task_waiting_approval,
                                                 ecom_draft_revised, task_approved /
                                                 task_rejected, ecom_publish_recorded
state/task_status.json                           approved_by / rejected_by, version, channel
```

Each audit record stores source (sample refs, dataset, generator
provider/model, human-edited flag), version + content sha256, operator,
approver, UTC time, publish channel, `publish_mode:
simulated_record_only` and `external_call: false`.

Remove demo tasks from the queue/status (artifacts and events are kept
as the append-only trail):

```bash
python3 commerce_demo.py --reset
```

Drafts created by v0.18.0/v0.18.1 were written into `task_queue.json`.
Copy them into the demo queue (task_queue.json is left unchanged; tasks
are de-duplicated by id):

```bash
python3 commerce_demo.py --import-legacy
```

### Import real store data (CSV, v0.19.0; review fixes v0.19.1)

Replace the sample products, orders and traffic with your own store
data. Export each Google Sheet tab as CSV (File → Download → CSV). The
header row must contain exactly these columns (order and letter case do
not matter; a UTF-8 BOM from Sheets/Excel is fine; empty trailing header
cells such as `...,category,,` and their empty cells are ignored):

```text
products.csv  sku, name, price_hkd, stock, category
orders.csv    order_id, date, sku, quantity, amount_hkd
traffic.csv   date, page, pageviews, source
```

Rules: `date` is `YYYY-MM-DD`; `price_hkd` / `amount_hkd` are numbers
≥ 0 (`HK$` and thousands separators are accepted); `stock` / `pageviews`
are whole numbers ≥ 0; `quantity` is a whole number ≥ 1; every order
`sku` must exist in products.csv. Duplicates are rejected: `sku` in
products, `date + page + source` in traffic. In orders, rows with the
same `order_id + sku` (and the same date) are merged, not rejected: their
`quantity` and `amount_hkd` are added together (a common export shape,
e.g. one row per variant or discount line) and the report counts the
merged rows; the same `order_id + sku` with a different date is rejected.
Limits: 5MB and 20,000 rows per file (rows over the limit are skipped and
counted as rejected in the report), 16MB per upload request.

Encoding: UTF-8 (Google Sheets, or Excel "CSV UTF-8 (Comma delimited)")
is preferred. A file that is not UTF-8 is retried as Big5 (cp950, then
big5hkscs), which is what Traditional Chinese (zh-HK/zh-TW) Excel saves
as plain "CSV"; the report shows the encoding used. If none works, the
file is skipped with a message asking you to save it as 「CSV UTF-8」.

Option A (command line):

```bash
mkdir -p data/import                  # gitignored
cp ~/Downloads/products.csv ~/Downloads/orders.csv ~/Downloads/traffic.csv data/import/
.venv/bin/python commerce_import.py   # prints the validation report (zh-Hant)
.venv/bin/python commerce_import.py --lang en      # report in English
.venv/bin/python commerce_import.py --status
.venv/bin/python commerce_import.py --reset        # back to sample data
```

Option B (browser): open `/import` (nav → 數據匯入), choose one to
three CSV files and click **驗證並匯入**, or click **從資料夾匯入** to
read `data/import/`. The page shows a report per file (rows, imported,
rejected) and per row (row number, column, value, problem). It also has
a toggle between imported and sample data, and a delete button (asks
for confirmation first).

Behaviour:

```text
partial import   valid rows are imported; every rejected row is listed
bad header       that file is skipped (other files still import)
missing file     a file not supplied keeps its previously imported rows
products only    a products-only upload keeps the stored orders; orders whose
                 SKU is not in the new products are kept in the state file
                 but excluded from every metric, and a warning with their
                 count is shown in the report, on /import, Market Dashboard
                 and Campaign Engine (they count again once the SKU returns)
nothing valid    previous data stays; only the report is stored
stored in        state/ecom_import.json (gitignored), with imported_at_utc
```

While imported data is in use, every demo page shows a green
**真實匯入數據** banner with the import time instead of 示範數據:

```text
Sales Hub         products table = imported products (inquiries/leads: sample, labelled)
Content Studio    SKU list + drafts use the imported product rows
                  (unknown specs/shipping are written as [HUMAN TO CONFIRM])
Campaign Engine   sales by SKU, low stock vs velocity, traffic by source,
                  AI campaign suggestion for an imported SKU (audiences: sample)
Market Dashboard  revenue, orders, units, AOV, pageviews, conversion,
                  revenue by week, sales by SKU, days of cover, traffic by
                  source + AI insight suggestion (sample KPIs hidden)
Knowledge Base,   still sample data; each section says so
Lead Desk
```

Metric definitions: days of cover = stock ÷ (units sold in the 30 days up
to the latest order date ÷ 30), flagged low under 14 days. Conversion =
distinct orders ÷ pageviews on the dates covered by both files; pageviews
are not unique visitors, so it is a rough ratio (the page states this).
AI suggestions use the same OpenRouter path (one call per click, mock
fallback). Their prompt carries a compact metrics summary sized to fit
3,000 characters (revenue totals, conversion, top SKUs by sales, every
low-stock SKU up to 20, traffic by source, recent weeks); product names
and other CSV text are stripped of control characters, length-capped,
quoted and placed in a delimited data block that the model is told is
data, not instructions. Ordinary chat questions keep the 800-character
limit. The drafts are drafts: they land in the Approval Inbox and nothing goes
out without a named approver. The import itself makes no model or
network calls.

### Tests

```bash
python -m unittest discover -s tests
pytest            # conftest.py + pytest.ini make this work from a fresh clone
```

### Limits (demo only)

```text
sample data unless you import CSVs (v0.19.0: products, orders, traffic only)
no external publishing, ads, email, WhatsApp or marketplace calls
lead scoring / classification is rule-based (stand-in for AI assist)
local accounts on one machine (v0.20.0); no OAuth / SSO / 2FA
```

## Accounts and governance (v0.20.0)

Every page now needs a signed-in account. There is no public sign-up.

### First start (bootstrap)

```bash
cd ~/my-orch-v0
.venv/bin/python orch_auth.py create-admin      # asks for username + password (hidden)
.venv/bin/python orch_ui.py                     # restart the console
```

Until an admin exists, every page shows a setup notice with that command.
`create-admin` refuses to run once any account exists. Passwords: 10-128
characters, stored as salted hashes (werkzeug), never logged.

### Roles

```text
admin     manages accounts, module approvers, retention, 權限清單;
          cannot create or approve drafts
editor    creates drafts, edits (new version), imports CSV data
approver  approves / rejects drafts in the Approval Inbox
```

The operator and approver in drafts, the audit log and `task_status.json`
are the signed-in usernames. Typed names on the forms are ignored.

### Rules enforced in code

```text
no self-approval   anyone who created or edited a version of a draft
                   cannot approve it (checked in commerce_demo.decide AND in
                   mini_orch.decide_approval via requested_by)
module approvers   /admin/approvers assigns approvers per module; a module
                   with nobody assigned can be approved by any approver
deadlines          each draft gets a deadline (4h / 24h / 48h / 72h / 7 days;
                   default in /admin/retention). Overdue drafts are flagged
                   in the Inbox and a reminder banner on the dashboard lists
                   who can approve them (escalation is on-screen only; no
                   email / chat is sent)
account changes    creating users, role changes, disable / enable and password
                   resets are requested by one admin and applied only after a
                   DIFFERENT admin approves (/admin/users). While only one
                   active admin exists, changes apply immediately and are
                   marked "single-admin exception" in the governance log.
                   The last active admin cannot be disabled or demoted; admins
                   cannot change their own account.
sessions           idle timeout (ORCH_SESSION_IDLE_MINUTES) and an absolute
                   limit (ORCH_SESSION_MAX_HOURS, default 12); signing out
                   or any change to an account ends that account's sessions
                   in every browser (a copied cookie stops working)
lockout            too many failed sign-ins lock the account for a while;
                   unknown usernames are counted and locked the same way,
                   the page shows one generic "sign-in failed" message and a
                   password hash is checked on every path (no username
                   enumeration). Admins can unlock in /admin/users (audited)
```

First-run rule for the single-admin exception: it applies only until two
active admins exist for the first time. After that it never comes back
on its own, even if only one admin is left: changes wait for a second
admin. To re-open it on purpose (for example the other admin left), run
`.venv/bin/python orch_auth.py allow-single-admin` on this machine; it
is audited with the OS user and closes again when a second admin exists.
The account a change targets can never decide it, so with exactly two
admins, removing one needs a third admin (or the CLI recovery above).

Authors can neither approve nor reject their own draft. The governance
audit section on /audit is shown to admins only; failed sign-ins for
unknown usernames store only a short hash of the typed name. The
權限清單 CSV neutralises cells starting with = + - @ tab or CR (leading
apostrophe) and shows times in HKT with an explicit +08:00 offset
(`ORCH_DISPLAY_TZ` to change).

Shell access to this machine is trusted: `orch_auth.py` CLI commands
(create-admin, unlock, reset-password, purge, allow-single-admin) and
`mini_orch.py approve` are not behind the web login. They record the OS
user (`getpass.getuser()`) in the audit trail.

### Retention and purge

`/admin/retention` sets how long decided (approved / rejected) drafts and
chat uploads are kept (defaults 180 days / 1 day). **Purge now** (or
`orch_auth.py purge`) removes decided demo tasks older than the limit,
their draft artifacts and old upload batches. Audit records
(`ecom_audit`), `state/events.jsonl` and the governance log are always
kept. Pending drafts are never purged. Purge is manual (no scheduler).

### 權限清單 (permissions list)

`/admin/permissions` lists every account, its role, status and the
modules it can approve. **匯出 CSV** downloads the same table (UTF-8 with
BOM, opens in Excel / Sheets). Each export is recorded in the governance log.

### Where it is stored (gitignored)

Since v0.21.0 both live in the SQLite state DB `state/orch.db` (mode 0600):
the `docs` row `auth` (accounts with password hashes, module approvers,
pending account changes, settings) and the append-only `auth_audit` table
(logins, lockouts, account changes, approver changes, purges, exports).
See "SQLite state (v0.21.0)" below.

### CLI (for recovery)

```bash
.venv/bin/python orch_auth.py create-admin [--username NAME]
.venv/bin/python orch_auth.py status            # list accounts
.venv/bin/python orch_auth.py unlock NAME       # clear a lockout
.venv/bin/python orch_auth.py reset-password NAME   # break-glass reset (logged)
.venv/bin/python orch_auth.py purge             # run the retention purge
.venv/bin/python orch_auth.py allow-single-admin    # re-open the single-admin exception (audited)
```

`python3 mini_orch.py approve` still works for normal (non-demo) tasks.
It is a local CLI and has no accounts. Demo tasks are refused there, as
before.

### Environment variables

```text
ORCH_UI_SECRET_KEY          session signing key (set a fixed value so
                            sessions survive restarts)
ORCH_SESSION_IDLE_MINUTES   idle sign-out, default 30
ORCH_SESSION_MAX_HOURS      absolute session lifetime, default 12
ORCH_DISPLAY_TZ             time zone for exported times, default Asia/Hong_Kong
ORCH_LOGIN_MAX_FAILURES     wrong passwords before lockout, default 5
ORCH_LOGIN_LOCKOUT_MINUTES  lockout length, default 15
SESSION_COOKIE_SECURE       1 = cookie only over HTTPS (set behind HTTPS)
ORCH_AUTH_DIR               directory of the DB holding accounts / audit, default state/
```

### Existing data

No migration is needed. Drafts and audit records made before v0.20.0
keep their typed names and show a "legacy (typed name)" tag. A pending
legacy draft can be approved by an approver whose username differs from
the typed creator.


## SQLite state (v0.21.0)

All mutable state now lives in one SQLite database, `state/orch.db`
(WAL journal, `busy_timeout` 30 s, `PRAGMA user_version` / `meta.schema_version`
= 1). The code still names state by the old file paths; `orch_db.py` maps them:

```text
state/task_status.json      -> table task_status (one row per task)
state/ecom_demo_queue.json  -> docs['ecom_demo_queue']
state/ecom_import.json      -> docs['ecom_import']
state/auth.json             -> docs['auth']
state/events.jsonl          -> table events      (append-only, UPDATE/DELETE blocked)
state/auth_audit.jsonl      -> table auth_audit  (append-only, UPDATE/DELETE blocked)
state/chat_usage.jsonl      -> table chat_usage
```

An approval (Inbox or `mini_orch.py approve`) is one `BEGIN IMMEDIATE`
transaction: a conditional update (`... WHERE approval_status = <what was
checked>`) plus its event row, so two concurrent approvals of the same
draft can never both succeed (tested with two processes).

**Still plain files** (by design): `task_queue.json` (hand-edited task
definitions, git-tracked; `mini_orch.py add-task` writes it), `state/tasks.json`,
`policy_contracts.json`, `demo/sample_data.json`, `artifacts/` (immutable
content-addressed objects + manifests), `uploads/`, `data/import/` CSVs,
`output/`, `state/secret_key`, `state/branding.json`.

### Migration from v0.20.x

Automatic, **only into an empty DB**: the first time the DB is opened (app
start, CLI, any read), every legacy JSON file in `state/` is moved into
`state/json-backup-<UTC>/` and imported in the same transaction (on any
error the files are moved back, nothing is written and no empty `orch.db` /
backup folder is left behind). Once `orch.db` holds data, a JSON file that
appears in `state/` is **refused**: it stays where it is, nothing is
imported or deleted, and an `ERROR ... REFUSED to import legacy JSON` line
is logged (once per process). To import such files on purpose:
`orch_db.py migrate --force` writes `state/orch.db.pre-migrate-<UTC>` (0600)
first, then upserts task rows (never deletes), replaces documents and appends
only log records not already present (deduplicated on their canonical JSON,
so re-importing a log or an export adds nothing). Explicitly:

```bash
# stop the UI first (Ctrl-C), then
cp -a state state.pre-v0210-backup          # extra safety copy
.venv/bin/python orch_db.py migrate         # empty DB only; prints what it imported
# .venv/bin/python orch_db.py migrate --force   # non-empty DB: backs up orch.db first
.venv/bin/python orch_db.py status          # schema version, row counts, migrations
.venv/bin/python orch_db.py check           # PRAGMA integrity_check
```

### Rollback to v0.20.x

```bash
# stop the UI, then write the DB back out as the old JSON files
.venv/bin/python orch_db.py export /tmp/orch-json
mkdir -p state/v0210-db && mv state/orch.db* state/v0210-db/
cp /tmp/orch-json/* state/
git checkout <v0.20.x commit>
```

(or copy back the files from `state/json-backup-<UTC>/` / `state.pre-v0210-backup`,
which hold the state exactly as it was at migration time).

`mini_orch_v1..v4_*.py` (historical single-file versions) read the old JSON
files, so they refuse to run once `state/orch.db` exists.

## Docker deployment (v0.21.0)

```bash
cp .env.example .env          # add OPENROUTER_API_KEY (optional) - never baked into the image
docker compose up -d --build
open http://127.0.0.1:5050/setup    # create the first admin (only while no account exists)
```

- Image: `python:3.12-slim`, non-root user `orch` (uid 10001), `HEALTHCHECK`
  on `GET /healthz` (unauthenticated; returns version + DB status only),
  served by waitress (`serve.py`).
- Volumes: `state/` (orch.db, secret_key, branding.json), `uploads/`,
  `data/`, `artifacts/`.
- Session key: `ORCH_UI_SECRET_KEY` if set, otherwise generated on first
  start and kept in `state/secret_key` (0600).
- First admin: `/setup` wizard (CSRF, only while no account exists; optional
  `ORCH_SETUP_TOKEN`), or `docker compose exec orch python orch_auth.py create-admin`.
- Branding: `ORCH_CLIENT_NAME`, `ORCH_LOGO` (https URL or a path inside
  `state/`, e.g. `branding/logo.png`), `ORCH_TARGET_MARKET`, or the same keys
  (`client_name`, `logo`, `target_market`) in `state/branding.json`. The
  version is shown in the footer.
- HTTPS / reverse proxy: `SESSION_COOKIE_SECURE=1`, `ORCH_PROXY_FIX=1`,
  `ORCH_TRUSTED_HOSTS=orch.example.com`; Caddy / nginx examples in
  `docs/反向代理與HTTPS.md`.
- Backup / restore: `scripts/backup.sh` (SQLite backup API snapshot + tar of
  the volumes) and `scripts/restore.sh <archive>`; `--local` for a plain checkout.
- Upgrade: `git pull` (or pull the new image), `scripts/backup.sh`,
  `docker compose up -d --build`; schema migrations run on start.
- Smoke test: `scripts/smoke.sh` (Docker) or `scripts/smoke.sh --local`.

zh-Hant guides: `docs/安裝指南.md`, `docs/使用手冊.md`, `docs/SOP.md`,
`docs/反向代理與HTTPS.md`.
