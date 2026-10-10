# Hosted vault dashboard: first release

This is the approved application extension for the planned `vault.naurolabs.com`
surface. It runs under the existing `/api/tasks` shell and Microsoft-owner BFF,
not a new web stack or identity. A checkout, passing test or PR is not evidence
that this domain, sign-in configuration or application is deployed.

Navigation is **Today | Tasks | Knowledge | Areas & projects | Activity**.
Task attention remains the Needs you filter inside Tasks. Existing task/API/auth
routes remain compatible. The shell supports `#today`, `#tasks`, `#needs`,
`#knowledge`, `#areas` and `#activity`; no source text belongs in URLs.

## Today

The display-only `home.md` projection admits non-sensitive rows under explicitly
approved/confirmed focus headings. It does not return the home document, private
pointers, the North Star's draft text, or proposed/annual goals. A draft North
Star is labelled separately. Source-recorded date windows stay attached to their
focus: expired means expired, upcoming is not current, and missing dates are not
invented. At most five eligible rows are displayed.

Task dates, attention eligibility and the owner calendar come from the existing
TaskService overview. Today does not independently interpret deadlines or turn
waiting into urgency. Pending proposals, unreviewed preparation and loaded
waiting tasks remain separate from calendar attention. A loaded page is not the
entire backlog; the existing 96-definition assessment and twelve-task pagination
remain unchanged.

Eligible `wiki/sources/` captures and canonical research are compared with a
previous immutable Git tree. New/updated means a new path/changed blob, not a date
guessed from its filename. Recorded source dates and UTC observation time are
shown separately. First use (or a baseline older than 35 days) shows an explicit
initial selection, never a claim that all those sources appeared today.

**Record this visit** explicitly advances the next comparison to the displayed
snapshot, including its unshown portion. It is not a read receipt or proof of
understanding. One private `task_workspace.dashboard_visit` contains just a
revision and observation timestamp in `briefing-state-v1.json`. A fifteen-minute
encrypted observation token and previous-marker comparison inside the ETag/CAS
update make repeats idempotent and refuse an older tab's overwrite. No browser
storage or per-note reading history is added.

## Knowledge inbox and evidence

The only generated-review display formats are:

- `reviews/vault-evolve/YYYY-MM-DD/review.json`: persisted receipt v1, validated
  independently of the model output schema. Exact fields, enums, date, IDs,
  proposal-only status, one-to-three citations per finding, SHA-256 source bytes
  and literal quotations are checked. Connections need two distinct sources.
- `reviews/digests/YYYY-Www-digest.md`: the canonical weekly digest generator's
  `type: digest`, period, generated date, `generated-by: vault-digest`,
  `status: draft`, known heading/boundary structure and relative note links.
  This means digests, not arbitrary personal weekly reviews or journals.

The digest contract was checked against mindVault's `.github/scripts/digest.py`,
`compose_markdown` at revision `90306fd`. Canonical publication does not turn its
draft status into approval. A digest has no per-source revision manifest: its
links are validated against current eligible sources, not presented as proof that
historical summaries are still correct. Unfiltered coverage counts and technical
activity are not copied into the reader.

Open a finding to see its exact evidence (one interaction). Open a cited source
from there (two). Each read rechecks a canonical Git tree, regular-file mode,
returned blob revision and current privacy eligibility. The client submits only
a host-derived opaque ID and the displayed blob revision. It cannot choose a
repository, branch, arbitrary path, URL or redirect. Source links inside prose
are not an alternate external reader; unvalidated references are withheld.
Markdown is rendered as text/limited DOM structure, never inserted as raw HTML.
Reference filtering happens before title/excerpt truncation. Unsupported
reference-style Markdown and HTML links are withheld rather than bypassing the
inline/wiki-link validator.

Privacy applies to titles, metadata, dates, snippets, references and bodies.
An invalid/private/missing or changed source withholds its dependent daily review.
A digest with an ineligible reference is withheld rather than exposing an
unfiltered catalogue. Only generic limitations are returned for withheld
material, without its identity or count. GitHub outages return an explicit
service failure rather than an empty inbox.

Bounds per source projection: at most 32 file reads, 64,000 bytes per file and
512,000 bytes total; existing 5,000-entry complete-tree bound remains. The inbox
pages four review artifacts; Today pages twelve changed source candidates.
Only authorized items are counted in the UI. Source/digest text is capped at
10,000 characters with an explicit shortened state. Limits and source errors
mean partial, not complete coverage. The page cursor is not a claim that skipped
private content was read.

These are **display adapters**, not new evidence inputs for generation.
`_kind`, `_REVIEW_DERIVED` and the existing daily-generation privacy/publication
contract are not broadened. A review or digest cannot feed itself back into
vault-evolve as an original source.

## Feedback and making a task

Useful, Already know, Not useful and explicit date snoozes share
`ReviewFeedback` with Telegram. They write the existing private
`vault-evolve-state-v1.json`, use its fourteen-day retention/record ceiling, and
require current evidence plus the displayed feedback version. Feedback remains
disabled when `MINDME_DAILY_EVOLVE_ENABLED` is off. Missing/changed private
receipts, expired windows, conflicting updates and persistence failures are
explicit. A canonical-only feedback receipt is not a Telegram delivery.

Making a task is separate:

1. Select a finding or original source, then edit a capture.
2. Review the exact record, including server-retained source paths, revisions
   and literal finding evidence in its context.
3. Explicitly approve that exact action through TaskService.
4. Observe the existing separate `publish_task` receipt and canonical readback.

Weekly digests require selecting an original source first. Captures exceeding
the existing context/action size bound fail explicitly; choose an individual
source instead of silently trimming evidence. Source revisions and daily
evidence are revalidated before execution/publication; stored drafts/history
are redacted if they cease to be eligible. Stable request/proposal/publication
IDs prevent a repeated request from creating a replacement task. Neither marking
a finding useful nor opening a source creates anything.
Automatic receipt maintenance applies the same evidence recheck before any
publication-capable reconciliation. Revoked evidence leaves an unresolved,
content-free operation receipt; it does not issue another publication attempt.

## Authenticated routes

All paths below are relative to `/api/tasks/api/dashboard`. Existing
`MINDME_WEB_ENABLED`, task/action feature gates, exact HTTPS origin, personal
owner session and CSRF rules apply. All responses are private/no-store.

| Route | Method | Contract |
|---|---|---|
| `/today?offset=N` | GET | Safe focus, source changes, observation and bounded-page state |
| `/inbox?offset=N` | GET | Validated canonical reviews/digests and generic limitations |
| `/read` | POST | `{id, revision}`; same-origin evidence/source read |
| `/visit` | POST | `{token}`; explicit CAS-protected visit marker |
| `/feedback` | POST | `{id, revision, finding, value, version}`; `review_on` required only for snooze |
| `/capture` | POST | `{id, revision, finding, request_id, text, definition}`; preview only |

There is no new timer, DB, model call on refresh, service worker, offline cache,
external asset or frontend analytics. The app never reads the laptop's Personal
OS, a work/customer vault, familyVault or raw originals.

## Proof and release boundary

`python -m pytest` includes `harness/tests/test_dashboard.py` regressions for
display/generation separation, metadata/body/link privacy, schema and source
drift, pinned regular files, draft/expired focus, bounded visits and CAS,
shared feedback, owner/CSRF denial and exact task preview/approval/retry behavior.

`node --check harness\web\tasks.js` and `node --check harness\web\dashboard.js`
check browser-module syntax. With an already-installed Python Playwright and
Chromium, run `python scripts\dev\check_dashboard_ui.py --artifacts <session-path>`
for labelled synthetic 1280px/390px screenshots and interaction assertions.
It intercepts all data routes; it neither logs into nor mutates production.
The loopback-only helper and browser stop before it returns.

Full connected library, ideas triage, Save as idea, new research UI and curation
remain future releases. Domain/DNS cutover, infrastructure, identities, secrets,
configuration, merge and deployment are outside this implementation's scope.
