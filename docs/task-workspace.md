# Personal task workspace

This is a single-owner task surface on the existing Functions host, alongside
Telegram. It is not a public app, a second task database, or a new executor.
The application and synthetic tests are local implementation evidence, not
evidence of deployed sign-in or a completed personal task.

The [vault dashboard extension](vault-dashboard.md) adds Today and Knowledge to
this same shell. Tasks, Areas & projects, Activity, the owner BFF and all existing
task routes remain in place. `/api/tasks#needs` opens task attention;
`#tasks`, `#today`, `#knowledge`, `#areas` and `#activity` select the other views.

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
Malformed or unavailable records produce explicit coverage limitations; unavailable
GitHub data is not an empty task list. Positive scope/privacy exclusions are
reported separately: an explicitly routed/private source or a source matching
the existing sensitive-content filter is outside this permitted view, not a
failed read. Excluded contents and filenames are never returned.

`attention_complete` is distinct from display pagination. Above the 96-source
assessment bound, or with unassessable metadata, it is false and the warning says
an empty Needs-you view does not establish that nothing is due. The content-free
`attention` metadata distinguishes attempted reads from successfully assessed
definitions and reports the window and `next_cursor`. Passing that
cursor as `offset` assesses the next window; ordinary `next_offset` visits its
twelve-task pages first. No cross-request task index, extra model call or new
database is introduced. Canonical/source eligibility checks still precede every
display and action.

`attention.scope` is `permitted`. Its `excluded_count` counts known policy
exclusions in the assessed window, while `unavailable_count` counts unresolved
read, schema or policy errors. A complete assessment means all permitted
candidates were accounted for, not that excluded private/routed work was assessed.
Ambiguous metadata, unsupported policy syntax, malformed dates, source revision
mismatches, size limits and network failures remain explicit errors and keep
coverage incomplete. The existing admission filters are unchanged.

Project pages make the same distinction using `excluded_count` and `errors`;
the overview exposes `project_excluded_count`, `project_candidate_count` and
`project_errors`. One project read failure does not discard independently
verified task data. Pagination preserves accumulated project exclusions and
unresolved errors across task-page appends; a fresh overview retries the source
snapshot. Known exclusions are described as out of scope, not an unavailable
project inventory, and unread pages still have a continuation control.

The portal presents known exclusions in one muted, initially collapsed
"Some sources are outside this view" disclosure. Its separate counts describe
task sources in the assessed window and project sources checked so far, never
a combined full-vault total. Names and contents stay hidden. Read errors,
ambiguous policy and incomplete coverage remain visible outside the disclosure;
unknown error codes use a content-free warning. EN/RU changes update this copy
locally without fetching sources. The disclosure retains its open/focus state
through same-session refreshes and paging, has no browser persistence, and clears
when the overview is absent or the owner signs out/session expires. It remains
part of task-source reporting, not Knowledge-source diagnostics.

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

The authenticated overview also provides navigation to the existing canonical
`home.md` dashboard and `wiki/index.md` knowledge index when those exact regular
files exist in its pinned Git tree. URLs use the configured repository and
canonical commit, not a hardcoded owner or a browser-selected destination.
These are external GitHub links requiring the owner's vault access, not new
in-app knowledge/dashboard views. No page content is fetched or copied into the
task response. Links are cleared on sign-out or when a fresh snapshot has no
matching regular-file navigation targets.

The task reader, clarification service, Microsoft auth adapter, Telegram adapter,
and web transport are respectively `task_sources.py`, `task_service.py`,
`task_auth.py`, `task_telegram.py`, and `task_web.py`. They share
`BriefingStore`, `BriefingLoop`, and `ActionGateway`.

## AI assist

The optional `MINDME_ASSIST_ENABLED` flag defaults off. Assistance shares the
existing owner BFF, CSRF header (`X-MindMe-CSRF`), exact HTTPS origin, permitted
canonical task reader and memex writer. It does not grant access to private,
routed or sensitivity-filtered tasks. Model calls reuse the existing Foundry
project and managed identity, with `store=False`, bounded context/output and no
automatic SDK retry. Task text, audio and model output never enter logs or traces.

All assist routes have the full prefix `/api/tasks/api/assist/`:

| Suffix | Method | Input / result |
|---|---|---|
| `voice` | POST | Raw audio, `X-Request-Id` UUID; transcript, language and duration |
| `chat` | POST | UUID `request_id`, message (up to 4,000 characters), optional thread/task binding, language, voice flag |
| `step` | POST | UUID `request_id`, capture/detail/estimate/plan/next/execute/resolve/learn, optional task and input |
| `usage` | GET | Estimated monthly/daily EUR usage, caps, calls and configured model names |
| `undo` | POST | UUID `request_id` and server-issued `receipt_id`; safe inverse of a confirmed eligible edit |
| `change` | POST | UUID `request_id`, `path`, `revision`, `change: {"stage": "ready"}`; deterministic board move, no model call |
| `receipt` | POST | UUID `request_id`, `receipt_id`; fresh publication/Undo projection, no model call or new task operation |

Audio is at most 2 MiB and 120 seconds, validated before transcription. Supported
content types are `audio/webm`, `audio/ogg`, `audio/mp4`, `audio/mpeg` and
`audio/wav`. Language is detected automatically, including mixed English/Russian.
Unparseable recordings are rejected explicitly, not assigned a guessed duration.
MP4 validation supports non-fragmented and fragmented single-track AAC-LC,
including Safari-shaped `moof/trun` output and unknown initialization duration.
It derives bounds from track/sample times and exact media-data coverage, not
compressed byte size. WebM Opus without a Duration field and unknown-length
segments/clusters is supported, as are Telegram Ogg Opus, MP3 and PCM/float WAV.
Encrypted/multi-track containers, unsupported codecs and edit lists remain
explicit format errors. Synthetic Safari-shaped fixtures are checked; an
actual iPhone recording still belongs to authorized post-deploy acceptance.
Audio is never persisted. The same UUID must be retained after an uncertain
response: a new UUID is not a retry of the same operation.

Both task assistance and the existing Telegram transcription adapter use the
account-level `/openai/deployments/{deployment}/audio/transcriptions` route
with the [documented stable `2024-02-01` API](https://learn.microsoft.com/en-us/azure/foundry/openai/whisper-quickstart).
They reuse the existing authenticated client and configured deployment, not the
unsupported project-level audio route.
Provider failures are service errors, not invalid-request-ID errors; diagnostics
record only the exception type, never the provider message or audio content.

After sign-in, AI availability loads independently of the task overview, so a slow
task inventory does not hide Copilot and microphone controls. **+Capture → Record
a task** transcribes into the editable capture form, appending to any existing
draft without replacing its title or details. It never sends an assist-chat
instruction or creates a task automatically: the owner reviews the draft and
clicks **Add task** to save the ordinary capture. Source-bound Knowledge captures
retain **Review capture** and their separate approval. Closing or replacing the capture cancels its recording and
prevents a late transcript from filling another draft. Overlong transcripts
remain visible in full and must be shortened to the capture limit before review.
The separate Copilot microphone retains its voice-instruction behavior.

Chat and step responses contain `thread_id`, a short language-matched `reply`,
`language`, typed `cards`, at most four follow-up `suggestions`, and `usage`.
Private working threads retain at most twelve bounded turns for fourteen days
in `system/mindme/task-assist-state-v1.json` in the existing private container,
not browser storage. The sidecar has a 512 KiB ceiling, forty threads,
160 request receipts, 160 inverse-operation records, 200 Telegram bindings and
300 Telegram delivery guards. Capacity fails explicitly. Source-version
bindings must be rechecked before recalled or cached task content is exposed;
obsolete/private sources cannot become fresh authority through thread recall.
Unconfirmed non-content operation guards remain protected from silent retry.
Capture/detail produce a single batched task definition/edit. Estimates and
plans are drafts, not commitments or evidence of completion. Execute prepares
a work product (such as a checklist or email draft), never sends it or performs
an external consequential action. Knowledge is a learning/wiki candidate, not
automatic wiki publication. Board suggestions use the server's permitted,
bounded inventory; incomplete attention must not become "nothing is due."

The current strict memex contract has no `estimate_min` write field and does
not permit `context` or `subtasks` in an existing-task refinement. Assist
therefore returns estimate/full-plan cards without claiming those structures
were saved; an accepted next-action refinement uses the existing writer path.
Creation persists a generated estimate as plain text in accepted `context`
and plan steps in accepted `subtasks` (at most six), preserving supplied content
within the writer bounds. Learning
can be saved with verified closure. Persisting structured estimates or replacing
existing plan sections requires a coordinated writer contract/manifest update,
not an assist-side whitelist bypass. Every overview item has `estimate_min`:
a valid explicit canonical integer takes precedence; otherwise a single plain
context line such as `Estimate: ~45m` or `Estimate: ~1h 20m` is parsed into minutes
without inference. Missing, ambiguous, ranged or malformed context estimates
return `null`; capture quotations and fenced code are not estimate authority.

The owner's explicitly granted assist standing authority is controlled by
`MINDME_ASSIST_AUTO_APPLY` (default true while assist itself remains off).
Ordinary capture/refinement, next actions, stage and focus/review edits can be
approved server-side through the existing proposal/decision/publication chain.
Receipts identify `approved_by: assist-standing`. Memex native checks, exact
writer manifest and canonical publication gates remain mandatory. A submitted
PR, scan checkpoint or unknown result is not a saved change. An `applied` card
represents a standing-authority attempt, not publication by its kind alone:
`saved` is true only with `status: completed`, `publication_status: merged`
and a current canonical source. Pending cards have `saved: false`, raw receipt
status such as `submitted`, `executing` or `uncertain`, and no available Undo.
Their `receipt_id` is the existing 24-hex proposal ID. Poll
`POST /api/tasks/api/assist/receipt` with `{request_id, receipt_id}` and the same
CSRF/origin protection. It returns the chat-style envelope with a refreshed
`applied` card including `status`, `publication_status`, `source_status`,
`saved` and `undo_available`; only completed/merged/current confirms publication.
Reuse the poll UUID for the same receipt: reconciliation projects its current
state rather than freezing an earlier pending response. The legacy
`/api/tasks/api/reconcile` still returns the raw receipt and is unchanged.
Polling does not continue a closure scan; checkpoints require the existing
explicit continuation protocol. Reversible edits expose an Undo action bound to the actual
current canonical revision. Task creation and closure are not silently undone
by deletion or reopening.

Card `status` is one of `completed`, `submitted`, `uncertain`, `unknown`,
`conflict`, `failed`, `invalidated`, `executing`, `running`, `pending`,
`accepted`, `snoozed`, `in_progress`, `cancelled`; other values normalize to
`uncertain`. `publication_status` is `submitted`, `merged`, `conflict`, `failed`,
`in_progress`, `unknown` or `null`. `source_status` is `current`, `unavailable`
or `changed_or_removed`, with missing authority treated as `unavailable`.
Clients must check publication/source flags, not infer saving from card kind.

Deadlines must come from the owner's actual statement, not AI inference.
Unstated deadline changes and closure without owner attestation stay proposals.
An explicit statement that the selected task is done can authorize closure, but
the model cannot fabricate results or evidence. Required result/evidence and the
owner-calendar verification date still pass existing closure validation; missing
facts need clarification. Disabling auto-apply restores ordinary proposal cards
approved through `/api/tasks/api/decide`. Disabling assist does not erase writer
receipts or grant legacy paths extra authority.
Proposal cards expose `action_digest`, exactly the existing `approval_digest`;
send `{proposal_id, approval_digest: card.action_digest, decision: "approve"}`
to decide. The legacy `/api/change` remains proposal-only. Dragging to Done
cannot replace explicit completion attestation.

English and Russian Telegram task messages use the same assist service.
`/tasks` shows focus/needs-you, `/next` suggests next work, `/plan <task>` prepares
a plan, and `/task <text>` captures a task. Existing explicit `/note`, `/idea`,
`/diary`, research and knowledge commands keep their routes. Replying in a task
assist thread preserves its server-owned task context. A yes/approve/да/
подтверждаю reply can approve the latest pending assist proposal; it cannot
approve an unrelated capture. Telegram callbacks retain the exact owner/private
DM allowlist and actual bot-message binding.

Assist spending is separate from the existing three/day, forty/month preparation
and research limits, which remain unchanged. The assist ledger uses estimated
EUR costs from token usage and configured per-model prices. Monthly cap defaults
to EUR 12 and cannot exceed EUR 20; the daily assist allocation is a soft
threshold of monthly/20, not an additional hard call limit. Accounting periods
use UTC; owner-calendar dates are used separately for task context/attestation.
Reservations happen atomically before paid calls. These application estimates
are not an Azure invoice or a guarantee that combined services stay under the
owner-approved EUR 20/month total. Unknown calls retain their reservation rather
than masquerading as free successful retries. Capacity, spending, model and
request conflicts return explicit safe errors.

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

### Owner workspace UI

The static shell uses the **Dark kinetic studio** identity: graphite, warm ivory,
chartreuse and an original inline-SVG folding study on sign-in. Dark is the default
regardless of OS theme; the coherent warm-light counterpart is an in-memory
toggle. Native display/system fonts need no downloads. See `DESIGN.md` for the
actual tokens, composition and public design/platform references.
Today highlights up to three loaded, server-eligible tasks with their recorded
attention reasons; it is not a new commitment or a claim of complete coverage.
Missing/partial task coverage is not called a quiet day. A combined estimate
names the number of tasks with recorded estimates, not an invented full workload.
One shared priority selection avoids repeated Today cards. Exact task dates,
waiting dependencies and definition warnings stay attached to the displayed task;
remaining loaded attention and waiting work appear below, alongside decisions
and results. Counts and coverage remain bounded, not a whole-backlog claim.
Flow offers a list and six-stage board; dragging uses deterministic, source-bound
`assist/change` (or the existing exact refinement when AI is unavailable). Done
still opens evidence-based closure rather than accepting a drag as verification.
Knowledge, Areas & projects, standing preparation and Activity remain reachable.

The task copilot has outcome/done conditions, a seven-step AI rail, and an
in-memory instruction thread. The global copilot opens with Ctrl/Cmd+K, the
header (desktop) or the phone's bottom action bar; the bar is hidden at 1024px
and wider so it never covers tasks. Between 1101 and 1599px an open task becomes
a right-hand drawer below the measured header, over the horizontally scrolling
board. At 1100px and below the detail replaces the task list. Refresh remains
available on phones. Only one view is shown after rapid navigation; transitions
are limited to the work area, never the fixed chrome. All entrance/movement
animations respect reduced motion, and loading states have no endless shimmer.
Its English/Russian controls follow the browser language,
with an in-memory toggle. Manual edit, date, preparation, verified closure,
clarification, capture and project controls use the same local copy catalog.
Canonical text, quotes, user/model output, backend messages, protocol values,
field names and ISO dates are not automatically translated.
The header, phone action bar and forms reflow at 200% **text resizing** without
reducing the requested text size. This is not a browser-zoom claim. Operational
metadata uses a shared 0.75rem role. The phone bar's measured height determines
reserved bottom space, as the header's height already determines drawer offsets.
Assist uses the existing same-origin API, session,
CSRF and uncertain-request recovery boundary. Applied cards distinguish pending
publication from confirmed canonical saving, show exact diffs and receipt-bound
Undo. Proposal cards retain exact server-bound approval; spoken yes/approve/да
confirms only a single pending card in the current thread. Plan checkboxes track
this session's draft, not canonical completion. AI-disabled responses hide AI
controls and leave the manual task workflow available.

Task/reader focus and back/Escape return are explicit; navigation supports arrow,
Home and End keys. Capture restores its actual trigger, including the phone bar.
The initial task-loading pane also has a focused region and a visible Back
control. Leaving Tasks cancels a pending initial selection; a late response cannot
strand a phone in a loading pane when the owner returns to Flow. A fully loaded
selection remains available on return.
Voice/API errors are repeated inside the copilot so a modal never hides the
failure behind its backdrop. Keyboard/touch information controls are dismissable.
Background receipt reads retain the same-source composer and selected tab.
Manual form drafts for the current selection also retain values, checkboxes,
selected options and focus/caret across locale changes, tab changes and same-source
rerenders. Only this selected task's path+revision is retained in memory; answers
additionally bind the current clarification question token. Changed/withheld
sources visibly discard manual drafts rather than applying old input to a new
revision. Explicit close/new selection and sign-out/expiry clear them. No
cross-task draft store or browser persistence is added.
Changed source content is still invalidated immediately; draft/caret/scroll
restoration requires a fresh permitted task and unchanged selection intent.
Closing/navigating away while a request is returning cannot reopen the detail;
moving keyboard focus elsewhere cannot pull it back into the composer.
Deliberate view navigation establishes the heading or retained selected-detail
context. Arrival is explicit and separate from loading: bootstrap must not scroll
or take focus from a header control during an async initial read. On narrow
screens, ordinary view arrival keeps the primary navigation strip below the
header alongside the new context; task/evidence drill-down keeps its own focus
and scroll behavior. Locale-only rendering, receipt polling and background
refresh do not perform that arrival action. Normal signed-out/expired sessions
offer Sign in, not a second Retry action; genuine network/server failures still
offer recovery.
Existing expiry and uncertain-operation guidance remains explicit.

`applied` does not mean saved: canonical saving requires `saved: true`,
`status: completed`, `publication_status: merged` and `source_status: current`. Submitted/executing
receipts refresh through `assist/receipt` with their 24-hex receipt ID and one
stable request ID reused across that receipt's polls, with bounded backoff for at
most two minutes while visible (manual flows keep `/api/reconcile`). Uncertain/failed/invalidated receipts require
inspection; no replacement request or closure continuation is scheduled.
Source-invalidated card content is removed. Undo is exposed only from the
refreshed card's confirmed saving and server-provided availability. A change response can contain only
cards/usage, without fabricating an AI reply or thread.

Voice uses browser microphone permission and MediaRecorder (Opus/WebM, or MP4
where supported), stops at sixty seconds/two MiB, then displays the transcript
and sends it as an instruction. No browser speech-recognition provider is used.
The header/phone Stop control retains the original recording's task binding
even after navigating to another view; navigation never retargets that audio.
Optional speech synthesis is off by default. Chat, audio, task content and UI
preferences are never written to browser storage; sign-out/expiry clears them
and stops microphone tracks. No external assets, analytics or service worker.
For synthetic development only, serve this shell on localhost with `?mock=1`.
Mock responses never call the real API and fail explicitly for unsupported
operations; this flag cannot bypass authentication on the deployed origin.

`python -m pytest harness\tests\test_task_workspace_ui.py` runs offline Node
contracts and, when Python Playwright/Chromium are installed, real-browser
fixtures for the owner boundary, voice/errors, stable receipt UUIDs and Undo,
keyboard/focus restoration, source invalidation, sign-out and rapid navigation.
The viewport/theme/locale/motion cases include desktop, 390px phone, 320px narrow
screens and the 1024/1101/1599/1600px layout boundaries. Set
`MINDME_UI_SCREENSHOTS` to a session-artifact directory for labelled synthetic
screenshots; do not point it into the repository or use real tasks/audio.

## Routes

By default (`MINDME_WEB_REDIRECT_ORIGINS` unset or empty), `GET /` is a root-only,
anonymous 302 redirect to the fixed relative `/api/tasks` path. It loads no
owner-auth configuration, sets no cookies and returns no task data. Non-root
paths and other HTTP methods are not handled by that root binding. Task routes
remain below `/api/tasks`; platform-anonymous declarations do not make
authenticated task data public.

With [opt-in canonical navigation](#configuration-and-release-gate), GET root,
bookmarked shell and sign-in requests on a configured legacy HTTPS origin, or an
ASCII hostname-case variant of the canonical/legacy origin, redirect to fixed
lowercase canonical destinations without query data or cookies. The exact
lowercase canonical root retains its relative redirect; its shell/sign-in
behavior, static assets and non-navigation routes remain unchanged.

The host HTTP `routePrefix` is empty so the root can be bound. Every existing HTTP
route explicitly retains its `api/` prefix: health, Telegram, Function-key tools,
task assets, sign-in/callback and task APIs keep their public URL, method and
authentication contracts. The optional `{ignored:maxlength(0)?}` template uses
the Functions host's ASP.NET route constraint to match only the empty root path;
an empty template would instead default to the function name.

| Route | Method | Result |
|---|---|---|
| `/` | GET | Default/exact canonical: relative 302 to `/api/tasks`; opted-in legacy/ASCII host-case navigation: canonical shell redirect. No cookies or data |
| `/api/tasks` | GET | Static shell; opted-in legacy/ASCII host-case navigation redirects to the lowercase canonical shell |
| `/assets/tasks.css`, `/assets/tasks.js`, `/assets/dashboard.js` | GET | Static assets only, never redirected |
| `/auth/login` | GET | Exact canonical Microsoft code+PKCE flow; opted-in legacy/ASCII host-case navigation first redirects to lowercase canonical sign-in |
| `/auth/callback` | POST | Bounded form-post callback; exact owner validation |
| `/auth/logout` | POST | CSRF-protected session revocation |
| `/api/session` | GET | Authorized session CSRF token and expiry, no OAuth tokens/identity details |
| `/api/overview?offset=N` | GET | One task page, permitted project page, limits and proposal/result history |
| `/api/projects?offset=N` | GET | Bounded permitted project inventory |
| `/api/history?offset=N` | GET | Ten source-revalidated proposal/result receipts per page |
| `/api/task` | POST | Read one permitted canonical task from `{path}` |
| `/api/capture-save` | POST | The owner form's Add task click authorizes this exact new Clarify capture; prepare, claim and publish through the existing ledger/writer |
| `/api/capture`, `/api/refine`, `/api/change`, `/api/close` | POST | Prepare an exact action; no write until approval |
| `/api/prepare` | POST | Prepare a bounded private-planning or public-research proposal |
| `/api/decide` | POST | Approve/decline the exact proposal ID and action digest |
| `/api/reconcile` | POST | Reconcile/publish the same previously approved receipt |
| `/api/continue-closure` | POST | Explicitly continue one bounded closure step using its confirmed writer token |
| `/api/recover-closure` | POST | Recover the last closure request using its original action and last-sent token; never select the next checkpoint automatically |
| `/api/clarify`, `/api/clarify-proposal` | POST | Bounded definition questions and a separate exact refinement proposal |
| `/api/projects`, `/api/standing` | POST | Explicit active-project and narrowly scoped preparation authorization |
| `/api/review-result` | POST | Owner reviews current output, or explicitly acknowledges an unavailable result's content-free notice; never completes a task |
| `/api/dashboard/today`, `/api/dashboard/inbox` | GET | Bounded canonical dashboard projections; see the dashboard contract |
| `/api/dashboard/read`, `/api/dashboard/visit`, `/api/dashboard/feedback`, `/api/dashboard/capture` | POST | Same owner/origin/CSRF boundary for evidence reads, private visit/feedback and source-bound capture previews |

JSON fields are strict. Duplicate keys, invalid dates, unsupported fields,
oversized payloads, stale revisions, and malformed identifiers are rejected.
An API error says no success is confirmed; it never fabricates an empty live view.

## Capture, clarification, approval, publication

The ordinary phone/desktop capture form is one step: type a rough request and
click **Add task**. That deliberate owner-authenticated, origin/CSRF-checked POST
to `/api/capture-save` is permission to save **only this new task**, not to perform
the requested work. Multiple items and the original multiline text remain one
task in Clarify. No model call or clarification questionnaire is needed. An
omitted title uses the first meaningful line without list/heading markers; the
capture text stays unchanged. The optional title, area, existing project,
execution, outcome, next action and done
condition still work; an `execution: agent` value is metadata, not permission to
run. This route cannot set deadlines, move existing tasks, close/drop tasks,
attach caller-supplied source evidence or authorize purchases/research.
It does not depend on, enable or extend `MINDME_ASSIST_AUTO_APPLY`.

The route reuses capture validation, the existing task request ledger and exact
approval digest, the atomic claim, ActionGateway and memex's separate
`publish_task` operation. The ledger records `approved_by: owner-capture` and a
content-free `capture_save_digest` on the bounded request receipt. Repeating the
same request recovers its original pinned proposal/action/publication IDs, even
if the task contract has since changed; a different body or a legacy/source-bound
request ID cannot acquire this permission. Source drift before the original claim
still blocks execution. No unknown create operation is reexecuted. Post-claim
errors remain unconfirmed (503), not field rejections permitting a new capture.

The result shows the task title/request and a readable saving, submitted or
unconfirmed status; technical action/receipts are collapsed. **Task saved**
requires a completed operation, merged writer publication and a current canonical
readback. Receipt of the request does not prove whether the task is already
saved; the UI says it is checking, not that the task is definitely unsaved.
Ordinary success copy says "Added to your task list"; publication details remain
in the collapsed technical receipts. Submission, timeout or unavailable readback
is not success. A displayed ordinary capture receipt gets at most six automatic
same-receipt checks, at cumulative 2, 6, 14, 30, 62 and 122 seconds while idle and
visible (2, 4, 8, 16, 32 and 60 seconds between checks). This allows ordinary
publication to take over a minute without needing another owner click.
Confirmation or a failed/invalidated/declined/expired receipt stops the checks.
Closing the receipt or hiding the page pauses them without resetting the cap;
sign-out/session expiry cancels them. An unconfirmed HTTP response requires explicit
same-request recovery. **Check save status** remains an explicit way to
reconcile that receipt. Reopening capture while its save is unresolved shows that
receipt instead of a replacement form. A lost initial HTTP response retains the
exact request and input for **Recover the same request**; validation failure or an
incomplete receipt never resets the form. All browser state is session-only and
clears on sign-out/expiry; after reentry inspect Activity rather than recreate an
unconfirmed capture.

Legacy `/api/capture` remains prepare-only. **Make task** from Knowledge still
uses `/api/dashboard/capture`, source revalidation and a separate exact approval;
opening a finding or editing a suggestion is not the ordinary form's save intent.

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

Apart from the explicitly authorized ordinary capture above, the web shows the
complete action before separate approval. Approvals bind the source
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

Dashboard capture proposals bind the selected canonical source/review blob as
well as the exact action. The display adapter revalidates every daily finding's
source hash and literal evidence, again before approval/execution/publication.
The canonical path, blob revisions and quoted evidence are carried in the task
definition's context, never accepted as browser-supplied provenance. Cached
history follows the same source-aware projection; a changed or withheld source
cannot expose a cached draft or approve an old capture.

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

### Explicit bounded closure continuations

The approved resumable closure contract permits a finite proof set of at most
128 eligible blobs / 2,000,000 declared bytes, including the task and applicable
ignore policy. Each writer invocation retains its existing 100-read /
512,000-fetched-byte / 64,000-per-blob / 20-second work bound and 25-second outcome
window. There are at most six explicitly requested read-only scan passes, with
no automatic loop. Final materialization and publication keep their existing
per-invocation bounds and eight-file mutation cap. Only complete pinned proof
may authorize the already-approved write.

New close requests create a new proposal for that owner request ID. Repeating
the same request ID stays idempotent; an old conflict receipt is not reset,
repinned or reused. Source/canonical/inventory drift and exhausted scan limits
remain explicit conflicts requiring a fresh source-bound owner approval.

An intermediate writer response is HTTP 202, `status: in_progress`,
`phase: closure_scan`, `resumable: true`, `path: ""`, `scan_pass: 1..6`,
`scan_pass_limit: 6`, and a server-issued 32-lowercase-hex
`closure_continuation`. This is a saved checkpoint, not a submitted or closed
task. It exposes no candidate paths, cursor, proofs or private source text.
The next token authorizes one explicit step, including bounded final
materialization once all proofs exist; it does not authorize a seventh scan.

The owner clicks **Continue closure check**. The BFF verifies owner/CSRF,
current source, immutable approval/action digest and the observed checkpoint in
the same ETag update that claims the continuation request. It sends the SAME
approved `update_task` request/action ID, adding only the writer-issued token.
The token is not part of the approved mutation-body digest and cannot select
paths, pins or proof. Double clicks, stale tabs, retries of the same caller
request ID and late responses cannot advance or overwrite a newer checkpoint.

An uncertain checkpoint returns no resumable authority. **Recover last closure
request** uses the last token actually sent, not the next token in a later
receipt. A missing/stale token on an existing scan only reads durable progress.
If the last request never reached the writer, recovery can perform that same
original authorized step; it never silently proceeds to another step. Running
or uncertain writer checkpoints remain unconfirmed, not a success-shaped result.

Ordinary refresh, repeated approval, legacy reconciliation and existing timers
never continue a closure scan. They preserve its last receipt. Only the explicit
continuation/recovery routes can send a scan request. Once the writer returns
the existing `submitted` receipt with an actual task PR, the existing separately
identified `publish_task` path resumes; submitted still does not mean canonical.
That durable publication ID keeps the request in publication recovery even when
a later publisher response is uncertain and omits its PR URL. Timer reconciliation
keeps the original submitted receipt in `result` and records later publication
outcomes separately in `publish_result`; uncertainty cannot restart a scan or
strand the operation without a publication-recovery control.
All displayed receipts use the shared current-source projection, and removed
or ineligible sources cannot expose a continuation token or authorize a pass.

The caller adds no task store, model call, standing permission, budget refund or
change to the old conflict receipt. The writer keeps metadata-only bounded scan
checkpoints in its existing private operation receipt; it owns complete-inventory,
ignore-policy and final canonical fences. The final rollout must integrate the
matching writer contract before enabling live continuation; offline adapter
tests are not real-task closure evidence.

### Closure conflict diagnostics

A validated HTTP 409 closure receipt retains only a finite known error code,
never an upstream message, reference path, body or token. Besides the existing
scan codes, the caller preserves these writer validation/service refusals:

| Code | Meaning |
| --- | --- |
| `source_closed` | The source is already done, closed or cancelled; this is not revision drift or proof this request closed it. |
| `task_linked_path` | A required path is linked or has a non-regular directory ancestor. |
| `task_source_invalid` | A required reference has invalid text or metadata. |
| `task_source_not_allowed` | A required reference fails privacy or source-safety rules; it is not skipped. |
| `task_publication_proof_missing` | Required closure proof is missing or incomplete. |
| `task_context_limit` | A bounded reading or finalization limit blocks closure. |

Only `source_changed` and `task_closure_source_changed` identify source/revision
change. Inventory changes, inventory limits, invalid references, privacy refusals,
link/proof problems and other conflicts have distinct fixed UI copy. Unknown
codes remain `task_closure_conflict`, displayed as **Closure conflict**, not
**Source changed**. Telegram also avoids inferring source drift from other
closure conflicts. Content-free conflict codes survive source redaction and
tombstoning; source/action content and continuation authority do not return.

This does not relax receipt validation, expand HTTP 503 uncertainty diagnostics,
change terminal receipts into resumable work, or permit automatic retries.
A new attempt still requires a fresh source-bound proposal and owner approval.
Older generic receipts cannot be reconstructed. Better diagnostics do not
identify their underlying cause or establish a successful real-task closure.

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
The optional `dashboard_visit` holds one canonical revision and server observation
timestamp, with no titles, source inventory or content. The dashboard treats a
baseline older than 35 days as expired. Explicit visit writes use a short-lived
encrypted observation token and compare the previous marker inside the CAS
update; an older tab cannot overwrite a newer marker.

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
| `MINDME_WEB_ORIGIN` | One exact canonical HTTPS origin on the existing host; no trailing slash or proxy-header inference |
| `MINDME_WEB_REDIRECT_ORIGINS` | Unset by default; comma-separated exact legacy HTTPS origins for portal GET navigation only, never additional auth/API origins |
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

For the canonical-domain cutover, the proposed settings are
`MINDME_WEB_ORIGIN=https://mindme.naurolabs.com` and
`MINDME_WEB_REDIRECT_ORIGINS=https://vault.naurolabs.com`. This is configuration
guidance, not a claim that the cutover is deployed. The callback is then exactly
`https://mindme.naurolabs.com/api/tasks/auth/callback`; no separate redirect-URI
setting or second authentication origin is added.

Configured origins must use lowercase ASCII hostnames (punycode for international names),
without ports, credentials, wildcards, paths, trailing slashes, queries or
fragments. Spaces around comma-separated entries are trimmed. Empty entries,
duplicates and the canonical origin in the legacy list are invalid;
misconfiguration returns the content-free `auth_configuration_invalid` error.
An absent or empty setting retains the prior relative root redirect and public
static shell behavior. Configured navigation requires the existing complete
owner-auth configuration; missing configuration still fails closed.

Only GET `/`, `/api/tasks`, `/api/tasks/`, `/api/tasks/auth/login` and its
trailing-slash form redirect from a listed legacy origin or an ASCII hostname-case
variant of the configured canonical/legacy origin. Destinations are fixed
lowercase canonical shell/sign-in paths. All query parameters are discarded, including
OAuth codes, state, tokens and caller-supplied return URLs. Redirect responses
are non-cacheable, suppress the referrer, and neither read nor set cookies.
No fragment is placed in `Location`, so browsers retain existing bookmarks such
as `#needs`, `#tasks` and `#knowledge`; fragments remain browser-only navigation,
not redirect destinations or authorization. The actual request URL must name a
configured HTTPS origin; only ASCII hostname lettercase may vary for navigation.
Userinfo, ports, trailing dots, encoded/non-ASCII hosts and other obfuscations
are rejected, not normalized. `Host`/forwarded headers do not grant access.
Unlisted and HTTP navigation is rejected when this opt-in is configured. Exact
lowercase canonical navigation retains its existing behavior.

POSTs, OAuth callbacks, task/session/dashboard/assist APIs, static assets,
Telegram and Function-key tools are not redirected or replayed. Auth/data/action
routes still accept only `MINDME_WEB_ORIGIN`; owner checks and exact-origin CSRF
are unchanged. `__Host-` cookies remain host-only and are not migrated or shared.
Sign in afresh on the canonical host. An in-progress callback to the old origin
fails closed after cutover rather than forwarding an authorization code.

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

For preparation evidence, the host reuses the knowledge quote-candidate selector
on the already permitted canonical source. Metadata is excluded and every offered
quote must remain an exact substring of the original text. The strict response
schema offers at most twelve exact strings, at most 1,600 quote characters total,
as the `source_quote` enum; the model selects evidence instead of regenerating it.
No quote is synthesized, fuzzy-matched or replaced after generation. The existing
500-character local quote bound and exact canonical membership check still reject
nonconforming output, even if a provider fails to honor the enum.

No safe citable candidate means a failed preparation before constructing a model
client. The complete serialized provider request, including prompts, nonce fences,
context and the quote-bearing schema, must fit the existing 9,000-character
input bound. Candidate selection adds no source read, model call or retry, and
failed requests do not refund an existing reservation or rewrite historical
receipts. These generation constraints address the observed exact-evidence
rejection category, not a claim about unretained model output; real acceptance
still requires an explicitly approved, in-budget trial.

Local preparation validation translates only the shared text validator's known
`invalid_text` and `unsafe_text` failures into the existing safe failed-receipt
codes. Empty, oversized, wrongly typed or secret-shaped model strings remain
rejected; exact source quotation and all output limits are unchanged. These
definite local rejections no longer leave a preparation stuck as executing.
Charges remain reserved and replay cannot call the model again. Unrelated
validation exceptions are not swallowed, and historical receipts are not rewritten.
This fixes an offline-reproduced error-boundary defect; it does not reconstruct
the cause of older generic failure receipts or establish live AI acceptance.

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
`test_closure_scan.py::test_actual_closure_wire_export` similarly exports
original/continuation/recovery/publication requests through the real caller
adapters when `MINDME_TEST_CLOSURE_OUTPUT` names a session-artifact JSON file.
Independent review, merged commits, verified push, hosted checks, deployment
and authorized live acceptance are separate coordinator responsibilities.
