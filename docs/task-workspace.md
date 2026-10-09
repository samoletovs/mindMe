# Personal task workspace

This is a single-owner task surface on the existing Functions host, alongside
Telegram. It is not a public app, a second task database, or a new executor.
The application and synthetic tests are local implementation evidence, not
evidence of deployed sign-in or a completed personal task.

## One task truth

Permitted canonical Markdown in `tasks/` remains the open-work inventory.
`tasks/done/` is the completion signal. The reader pins the canonical Git tree,
requires regular files, checks returned blob revisions and applies source privacy
rules. It assesses up to 96 permitted task definitions at one pinned revision
before selecting each twelve-task display page. Real approaching deadlines,
today's explicitly dated focus and due reviews use one configured owner calendar
and the same seven-day deadline horizon for both ordering and eligibility;
capture age is not urgency. A new due task cannot be hidden behind
twenty old undated tasks merely because the first twelve filenames are older.
A display page is not the whole backlog.
Malformed or excluded records produce explicit coverage limitations; unavailable
GitHub data is not an empty task list.

`attention_complete` is distinct from display pagination. Above the 96-source
assessment bound, or with unassessable metadata, it is false and the warning says
an empty Needs-you view does not establish that nothing is due. The content-free
`attention` metadata distinguishes attempted reads from successfully assessed
definitions and reports the window and `next_cursor`. Passing that
cursor as `offset` assesses the next window; ordinary `next_offset` visits its
twelve-task pages first. No cross-request task index, extra model call or new
database is introduced. Canonical/source eligibility checks still precede every
display and action.

`MINDME_TASKS_TIMEZONE` is a required, validated IANA setting, not an inferred
browser or device zone. Missing/invalid configuration returns 503, without an
implicit UTC calendar. The overview's `attention.calendar_date`, `timezone` and
`deadline_horizon_days` describe the server-trusted owner calendar. Every item
has `attention_eligible` and `attention_reasons` using that same calendar and
horizon: `deadline_due`, `deadline_soon`, `review_due`, `focus_today`. The UI does
not re-sort or derive another date predicate. Future reviews/snoozes suppress
discretionary focus, never hard deadlines. Undated definition gaps and waiting
alone stay in All tasks/board, not automatically in calendar attention.

The top-level overview `date`, work-budget counters, approval/receipt/expiry
dates and auth lifetime remain UTC. Owner-reported completion `verified_on` is
validated against the owner calendar; host processing/completion dates remain
separately recorded UTC facts. No client-supplied date or zone controls limits.

The eleven area IDs and permitted project notes are inventories, not selected
commitments. The owner explicitly selects active projects. Tasks without a stage
remain untriaged; filled fields prove structure only. Ready/Doing/Waiting/Verify
are not inferred from capture age, a model suggestion, or `execution: agent`.
Deadline, review date, and dated focus stay separate. Yesterday's focus does not
become today's focus.

The task reader, clarification service, Microsoft auth adapter, Telegram adapter,
and web transport are respectively `task_sources.py`, `task_service.py`,
`task_auth.py`, `task_telegram.py`, and `task_web.py`. They share
`BriefingStore`, `BriefingLoop`, and `ActionGateway`.

## Sign-in and authorization

The BFF uses the supported MSAL authorization-code flow with S256 PKCE. It selects
one explicitly configured personal directory, never `common` or an inferred
consumer subject. PyJWT separately checks the Microsoft signing key, RS256,
issuer, audience, lifetime, and nonce. Authorization also requires the exact
configured `tid`/`oid` tuple and a signed Microsoft personal-account provider:
exactly `live.com` or
`https://sts.windows.net/9188040d-6c67-4c5b-b112-36a304b66dad/`.
Microsoft documents both forms in the
[ID-token claim reference](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference).
These equivalent providers normalize to `live.com` in the encrypted session;
neither permits another tenant or owner.
An authenticated work identity, another personal user, or a missing identity
claim does not become the owner. There is no first-visitor enrollment, bearer
header shortcut, local demo identity, or `X-MS-CLIENT-PRINCIPAL` fallback.

MSAL adds `openid profile`; `offline_access` is explicitly excluded. There are no
Graph mail, calendar, Files, or OneDrive permissions. OAuth tokens never enter
the browser or durable state. The ten-minute authorization-flow cookie is
encrypted, `__Host-`, Secure, HttpOnly, and SameSite=None because the callback
uses `form_post`, not a code-bearing query string. The one-hour session cookie
is encrypted, `__Host-`, Secure, HttpOnly, and SameSite=Lax. Only hashed replay
and session identifiers with expiry are saved privately. Logout revokes that
session. Concurrent callback replay is rejected using the existing ETag store.
Both encrypted cookies use unpadded base64url on the wire: the Functions Python
binding converts `Set-Cookie` to an RPC cookie, and the ASP.NET host URI-escapes
its value. Padding would otherwise become `%3D` in the browser. Reading also
accepts the previous raw or URI-escaped padding, restoring canonical Fernet
encoding before the same signature and lifetime checks. Existing valid sessions
can recover without clearing the ledger, changing keys or creating new sessions.
At the eight-session or forty-recent-sign-in bound, a verified owner receives
an explicit 429 response instead of a generic service failure. Expired receipts
are pruned before capacity checks; live sessions and replay receipts are not
silently evicted.

Each data/action route validates its actual HTTPS request URL against the exact
configured origin. Forwarded scheme or identity headers cannot bypass it.
Mutations require exact Origin and a per-session CSRF token. Source strings
travel in JSON bodies, not task-title query strings. Responses are no-store;
the frontend has no service worker, local storage, external assets, or analytics.
All source strings are text, not executable HTML. Existing webhook-secret and
Function-key authentication remain unchanged; do not enable global EasyAuth
across those routes.

## Routes

`GET /` is a root-only, anonymous 302 redirect to the fixed relative `/api/tasks`
path. It reads no configuration, sets no cookies and returns no task data.
Non-root paths and other HTTP methods are not handled by that redirect. Task
routes remain below `/api/tasks`; platform-anonymous declarations do not make
authenticated task data public.

The host HTTP `routePrefix` is empty so the root can be bound. Every existing HTTP
route explicitly retains its `api/` prefix: health, Telegram, Function-key tools,
task assets, sign-in/callback and task APIs keep their public URL, method and
authentication contracts. The optional `{ignored:maxlength(0)?}` template uses
the Functions host's ASP.NET route constraint to match only the empty root path;
an empty template would instead default to the function name.

| Route | Method | Result |
|---|---|---|
| `/` | GET | Fixed 302 redirect to `/api/tasks`, no cookies or data |
| `/api/tasks` and `/assets/tasks.css`, `/assets/tasks.js` | GET | Static shell only |
| `/auth/login` | GET | Microsoft code+PKCE redirect |
| `/auth/callback` | POST | Bounded form-post callback; exact owner validation |
| `/auth/logout` | POST | CSRF-protected session revocation |
| `/api/session` | GET | Authorized session CSRF token and expiry, no OAuth tokens/identity details |
| `/api/overview?offset=N` | GET | One task page, permitted project page, limits and proposal/result history |
| `/api/projects?offset=N` | GET | Bounded permitted project inventory |
| `/api/history?offset=N` | GET | Ten source-revalidated proposal/result receipts per page |
| `/api/task` | POST | Read one permitted canonical task from `{path}` |
| `/api/capture`, `/api/refine`, `/api/change`, `/api/close` | POST | Prepare an exact action; no write until approval |
| `/api/prepare` | POST | Prepare a bounded private-planning or public-research proposal |
| `/api/decide` | POST | Approve/decline the exact proposal ID and action digest |
| `/api/reconcile` | POST | Reconcile/publish the same previously approved receipt |
| `/api/clarify`, `/api/clarify-proposal` | POST | Bounded definition questions and a separate exact refinement proposal |
| `/api/projects`, `/api/standing` | POST | Explicit active-project and narrowly scoped preparation authorization |
| `/api/review-result` | POST | Owner reviews current output, or explicitly acknowledges an unavailable result's content-free notice; never completes a task |

JSON fields are strict. Duplicate keys, invalid dates, unsupported fields,
oversized payloads, stale revisions, and malformed identifiers are rejected.
An API error says no success is confirmed; it never fabricates an empty live view.

## Capture, clarification, approval, publication

Telegram `/task` still forwards the original integer update/message/chat/from
IDs to memex. A new task requires the enrolled DM owner; there is no sender
fallback. memex retains its explicit-command quick-capture authority.
`task1|clarify|<action-id>` and raw replies resolve the actual owner, confirmed
bot reply, and source pointer through the existing Function-authenticated
`MEMEX_WEBHOOK_URL` task-context operation. The pointer is not approval or
canonical evidence. mindMe independently checks the current canonical blob.
Pending publication or a changed blob cannot be refined through the old pointer.

Clarification asks one focused definition question at a time. The three-answer
limit is durable and source-version-bound. Answers populate only the field
actually asked about; a reply to an older question cannot fill the next field.
Each issued question includes an opaque `question_token`. Web answers submit
that exact token with `path`, `revision`, `answer`, and `request_id`; confirmed
Telegram question bindings retain the same token. Token/field/turn validation
and answer recording occur in one ETag update, including after a concurrency
retry. A stale tab/message is rejected without consuming a turn. Repeating the
same request and token remains idempotent after advancement.
The original Capture stays untouched. Remaining gaps stay Clarify. Full form
edits and collected answers still become an exact owner-reviewed proposal,
not silently expanded scope or automatic activity.

The web shows the complete action before approval. Approvals bind the source
blob, exact action digest, persisted proposal and authenticated owner. The
existing atomic claim precedes external effects. Stable request/action IDs
prevent duplicate writes; a different body cannot reuse an ID.
Disabling Tasks also disables approval of its already-created cards. Legacy
briefing callback/reply handlers reject task-workspace proposals, the approval
loop requires the TaskService claim gate, and the action gateway requires an
explicit live task-feature gate. Ordinary legacy briefing proposals retain their
separate existing behavior.

The generic memex `create_task`, `update_task`, and `refine_task` API remains
review-only. After the owner approves that exact action, the separate
`publish_task` request uses its own stable operation ID and `target_action_id`.
It accepts no caller-selected repository, PR, path or branch. The writer's
recorded manifest, canonical base, task-only diff/link repairs and native checks
govern its one ordinary merge attempt. No admin/force merge or checks bypass is
introduced here. Rechecking the same publication receipt is not submitting a
replacement task. Legacy receipts without the necessary manifest fail closed.

`submitted` is not saved. `in_progress` and unknown network outcomes are not
successful submission just because the HTTP status was 202. Only verified
canonical readback may report `merged`. Old proposal clicks, source changes,
ambiguous publication and interrupted delivery never grant a fresh operation.
Unknown sends are not automatically repeated.

One source-aware projection checks eligibility and revision before every stored
receipt response, including history, repeated proposal creation/approval,
publication and reconciliation. Changed, removed, or unavailable evidence yields
only a content-free receipt, never an approval control or cached private draft.
Completed source-removal tombstones keep immutable completion and operation
proof: rendering them does not invalidate completion or discard unresolved
receipts. Verified closure
checks the recorded closed file, not the now-removed open path.

## Bounded preparation and review capacity

The explicit preparation path is real: selected canonical task -> pending scope
card -> owner approval -> existing model or research executor -> private result
or submitted report -> observable reconciliation. A model draft is not a
verified result and does not close its source task.

Private preparation uses the existing briefing model, one call, no tools,
`store=False`, at most 9,000 input characters and 1,200 output tokens, a 45-second
work deadline, no regeneration/retries, five proposed steps and three
uncertainties. An exact source quotation is checked before accepting its output.
Task and standing-project revisions are checked around inference (at most four
bounded source reads during execution). A changed scope cannot certify the draft.
No email, booking, payment, purchase, application, security or medical-care
execution path is added. Public research uses the existing gateway: one explicitly
approved impersonal question, five public sources, one report of at most 1,200
words, no follow-on jobs, and no private task context in the research request.

The owner may enable standing permission for private preparation only, against
at most three specific canonical Ready task revisions in explicitly active
project revisions. Changing active projects resets it. Source/project drift or
revocation stops it. Existing morning/Sunday timers may prepare at most one such
task per day; unchanged work is not repeated. They do not select priorities,
create commitments or start public research. Completed preparation needs an
explicit owner review to release review capacity.

The non-content `reviewed` flag survives source closure/removal, so a result the
owner already handled does not consume capacity again. False/missing means no
review receipt, not proof that the owner did not read it. An unavailable completed
preparation/research result can be handled explicitly with
`POST /api/review-result {proposal_id, acknowledge_unavailable: true}`.
The UI labels this **Acknowledge unavailable result**, not reading private output
or verifying a task. It sets validated, monotonic `reviewed` and
`acknowledged_unavailable` flags, without restoring content, changing completion
proof or closing the source task. Ordinary review refuses unavailable output;
unacknowledged receipts still reserve capacity. Submitted/uncertain actions
cannot be dismissed through this completed-result acknowledgment.

Daily/monthly counters are reserved before inference or approved research, even
when a result is uncertain. Default limits are three operations/day, forty/month,
and three unreviewed results. Hard configuration ceilings are five/day,
sixty/month, and ten unreviewed results. These are application work limits,
not a promise that all existing Azure/GitHub usage costs less than EUR 10.
The release coordinator must verify the combined budget. No new model, premium
plan, service or recurring model call without saved standing scope is required.

## Closure and learning

Close prepares `update_task` with exactly `status: done` and an owner attestation
against the displayed Done-when condition. It requires an actual result,
evidence, `verification: owner`, and the owner-reported `verified_on` date;
optional learning is retained in the canonical closed task. That date is not
invented as the date the work happened. memex separately records processing
time, moves the file to `tasks/done/`, preserves Capture and existing dates,
clears open stage/focus, and repairs supported links or refuses closure.

Reading, signing in, an external outcome and human verification cannot be inferred
from a generated draft or a merged research report. Knowledge stays in the task's
Learning record first. Wiki promotion still needs the owning vault's recurrence
policy and review; no automatic wiki page or agent-instruction rewrite is added.

## Private operational state

`task_workspace` is an optional, backward-compatible part of the existing
`system/mindme/briefing-state-v1.json`, not another blob container/database.
It holds selected project revisions, standing scope, clarification fields,
request receipts, work counters, and hashed expiring auth metadata.
There are no canonical dates or task rows in it. Proposals/actions/results reuse
the existing proposal collection.

Caps are 100 clarifications, 300 question bindings, 300 non-content request
receipts, forty short-lived auth nonces and eight sessions, inside the existing
1 MiB total ceiling. Clarification content expires after fourteen days; expired
questions remain non-replayable rather than becoming a new capture. Capacity
fails closed. Unresolved action receipts are never silently evicted.
Source text, model output, task text, identity claim values and credentials never
go to logs or span attributes. A signed-token owner rejection logs only its
fixed check code: `tenant`, `owner`, `provider`, `client` or `nonce`. The public
response remains `owner_not_authorized`.
Rejected browser sessions log only a fixed cookie, sealed-payload, session-claim
or receipt check code; cookie values and private state are never logged.
An unreadable sealed cookie also records its length and Boolean prefix,
signature and lifetime checks, never its value, timestamp or decrypted payload.

## Configuration and release gate

| Setting | Meaning |
|---|---|
| `MINDME_TASKS_ENABLED` | Default false; new task adapters/maintenance require the existing action-briefing flag too |
| `MINDME_TASKS_TIMEZONE` | Required IANA owner calendar; no inferred zone or silent UTC default |
| `MINDME_WEB_ENABLED` | Default false; static shell and Microsoft BFF routes |
| `MINDME_WEB_ORIGIN` | Exact HTTPS origin of the existing host; no trailing slash or proxy-header inference |
| `MINDME_WEB_TENANT_ID` | Explicit personal directory, never a work tenant or common authority |
| `MINDME_WEB_CLIENT_ID` | Approved single-tenant confidential web app registration |
| `MINDME_WEB_OWNER_OBJECT_ID` | Exact owner object ID in that directory, not a guessed consumer subject |
| `MINDME_WEB_CLIENT_SECRET` | Key Vault reference to that registration's secret |
| `MINDME_WEB_COOKIE_KEY` | Key Vault reference to a 32-byte Fernet key; rotate to invalidate cookies |
| `MINDME_TASK_MODEL_DAILY_LIMIT` | Default 3; 1-5 |
| `MINDME_TASK_MODEL_MONTHLY_LIMIT` | Default 40; 1-60 |
| `MINDME_TASK_REVIEW_CAPACITY` | Default 3; 1-10 |

Existing `DIG_GITHUB_TOKEN`/`DIG_REPO`, `MEMEX_WEBHOOK_URL`, `MEMEX_ACTION_URL`,
`TELEGRAM_ALLOWED_CHAT_ID`, `MINDME_BRIEFING_MODEL`, private storage and managed
identity settings are reused. Never infer one Function route's key from another.
The redirect URI is exactly `${MINDME_WEB_ORIGIN}/api/tasks/auth/callback`.
Sign-in must produce one of the exact signed personal-provider `idp` values
above for the configured owner. Email addresses are not authorization keys.

No infrastructure or provisioning helper is part of this implementation.
Registration, the two secrets, HTTPS-only host configuration, runtime settings,
owner consent/sign-in, and manual existing-host publish remain explicit release
gates. A test/PR merge does not deploy this application. See [deploy.md](deploy.md).

## Offline proof

Private preparation uses the existing model's single-turn Chat Completions API
with `store: false`, no tools, no retries and a 1,200-token bound. Other agent and
briefing paths are unchanged. The provider schema uses Azure's supported strict
JSON Schema subset; array
limits are enforced by the existing local validator, not unsupported `maxItems`
keywords in the provider request. Explicit 400/422 provider rejections become
failed, retained receipts with no automatic retry or budget refund. Diagnostics
contain only the HTTP status and allowlisted code/parameter labels, never the
upstream message or task input. Repeating an approval cannot reexecute a claimed
preparation, including a failed one; another attempt needs a new reviewed proposal.
Transport uncertainty remains uncertain and is never treated as a successful draft.
Refused, truncated or missing completions are failed drafts, not successful output.
The failed receipt retains an allowlisted reason code for request rejection,
invalid output, unmatched evidence or source drift. Logs include only that code
and the fixed source-check/generation/validation/source-recheck phase, never
model output or arbitrary exception text. Unknown error text remains generic.

Run `python -m pytest` for the complete synthetic suite. New `test_task_*` tests
exercise actual MSAL flow construction/token exchange with generated signed
synthetic JWTs, owner/issuer/audience/key/lifetime/nonce rejection, cookies/CSRF,
canonical readers, TaskWeb transport, clarification/approval, bounded preparation,
unknown outcomes, and bot compatibility. No real login, Telegram send or task
execution is used.

`test_task_contract.py` captures real TaskService -> ActionGateway serialization
for create/refine/wait/focus/date-clear/close and separate publication operations.
Set `MINDME_TEST_CONTRACT_OUTPUT` to a session-artifact JSON path while running
that test to export its synthetic array for memex's real request validator.
Independent review, merged commits, verified push, hosted checks, deployment
and authorized live acceptance are separate coordinator responsibilities.
