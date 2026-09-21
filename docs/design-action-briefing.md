# Design: action-oriented personal briefing

Status: implemented, deployment approved, and live-verified 2026-09-13.

## Scope and boundaries

Use the existing morning and weekly timers, Telegram owner chat, private Blob
container, Foundry model deployment and memex capture/GitHub workflows. Add no
service, cron, vector database or public endpoint. Automatic work is limited to
reading allowed sources, synthesis and preparing proposals. Research, task creation
and task changes require an explicit, source-version-bound owner decision.

The rollout is opt-in through `MINDME_ACTION_BRIEFING_ENABLED`. Existing deployments
and section preferences retain their behavior until enabled.

## Components

- `harness/briefing_sources.py`: bounded, commit-pinned, non-sensitive GitHub
  context. Reads approved goals from the canonical dashboard, not private mirror
  headings or an unmerged local copy. Returns source revisions and normalized
  fingerprints; indexes, raw/private sources and routed content are excluded.
  Observed blob revisions and a scan cursor keep unchanged recent files from
  starving older material. This scan state is separate from presented-source state.
- `harness/briefing_plan.py`: typed/validated model output, deterministic urgent
  task handling, source citations and bounded presentation. Existing model
  deployment only, one synthesis call with at most one regeneration for invalid
  formatting or evidence binding, within the same invocation deadline. The
  regenerated plan must pass the same validation before any delivery or proposal
  receipt. Unsafe text, sensitive research, unavailable dependencies and delivery
  failures are not retried by this mechanism. No tool calls from the synthesizer.
- `harness/briefing_state.py`: private state with optimistic concurrency. Proposal
  claims precede side effects; no acknowledgement before durable persistence.
- `harness/briefing_loop.py`: delivery, message/reply binding, decisions, source
  invalidation, inspection/deletion and action reconciliation.
- `harness/weekly_review.py` and `harness/weekly_plan.py`: independent weekly
  comparison, bounded HTML presentation and up to three source-bound action cards.
  They reuse the existing proposal store, executor and `brief1|` approval callbacks.
- `harness/briefing_actions.py`: authenticated memex action client and bounded
  research dispatch, with content-free status errors and stable operation IDs.
- `harness/function_app.py`: thin integration into existing timers, Telegram
  callbacks, text/voice replies and commands.
- memex `/state`: reads real task metadata rather than capture-age filenames.
- memex `/personal_action`: Function-authenticated, enrolled-chat-only,
  idempotent create/update-task operations through existing reviewable writes.

## Storage and lifecycle

Markdown remains canonical for non-sensitive goals/tasks/knowledge. The existing
private container stores `system/mindme/briefing-state-v1.json`: proposals, decision
receipts, message bindings, concise corrections, delivered-source fingerprints and
delivery receipts. It does not duplicate the entire vault or retain chat transcripts.
Limits are explicit, with failures rather than silent eviction of unfinished work.

Proposals bind a source path, source revision, content digest and exact action.
Source changes invalidate approval. Duplicate/replayed decisions cannot start a
second operation. An uncertain external response stays uncertain until read-only
reconciliation; it is never retried as a fresh research job.

Delivery checkpoints advance only for sources actually presented after Telegram
confirms the entire delivery. Per-message receipts distinguish sending from saved
delivery; an interrupted send never certifies unseen material.
The first confirmed delivery also establishes a separate bounded semantic baseline.
It does not mark those source notes read or handled. Newly encountered older notes
are labelled new to the briefing, not falsely newly created. Interrupted deliveries
can resume with their persisted content and proposal identity; after a source
revision/day change they are abandoned and regenerated, not silently certified.

`/memory` exposes stored feedback, `/memory forget <id>` deletes it, and source
deletion removes derived feedback and invalidates pending actions. Declines and
snoozes suppress unchanged proposals; real task deadlines remain visible.

### Action-first weekly review

The action-enabled Sunday timer replaces both legacy count messages. `/review`
uses the same path. It compares with the last fully delivered **weekly** baseline,
not the latest morning briefing. First runs establish a baseline; an older file
first encountered now is not presented as work created this week.

Decision/result transitions now carry their observation dates, bounded to 32
entries per proposal and pruned after 35 days on source reconciliation. The
summary uses the last seven calendar days, or the days after a more recent review.
It shows the latest observation per action in that window. A first reconciliation
of a merged result is dated when verified, not backdated to approval or presented
as proof of when the real-world work happened. Legacy receipts have no invented
dates. Removed-source receipts retain date/status pairs but no derived text.

The model receives current safe canonical sources and concise scoped corrections;
private-mirror counts and journal facts are excluded. Mirror freshness metadata is
displayed independently. Current canonical sources can still support recommendations
when the private mirror is stale. Neither missing evidence nor a stale sync proves
inactivity. The existing 24-source bound and source-specific validators still apply.

An unchanged pending decision can occupy a weekly card after its revision and
expiry are checked. Remaining slots allow new proposals, with a hard total of
three and one separately bound approval per action. No execution is performed
during synthesis or delivery. A selected next action remains an open task.

Weekly sends claim and confirm each message separately. Unknown send outcomes
block automatic replay; `/review retry` explicitly accepts a possible message
duplicate without replaying action execution. Only a fully delivered review
advances its weekly baseline. Same-day successes have a CAS-assigned completion
order, so the latest delivered snapshot wins. Retention protects two successful
weekly records independently of abandoned attempts and eight prior daily records;
unresolved sends are not evicted. A concurrent completion cannot be downgraded
to an abandoned attempt.

## Permissions and external effects

The model can suggest only review-task, create-task or bounded-public-research
operations. It cannot grant permission, choose a repository, execute arbitrary
tools, change security, purchase, transfer funds or send to other recipients.
New callbacks use an isolated namespace; legacy memex note-review callbacks
continue through their existing handler. Ambiguous replies prompt clarification.

Task edits compare current source SHA. Completion is verified canonical state,
not just a PR being opened. Research issue acceptance is reported separately from
a merged report. Task changes use an explicit `MEMEX_ACTION_URL`; never assume
a function-specific key from another endpoint authenticates this route.

## Cost and rollout

Default feature off. Enable only with configured GitHub read access, explicit
model deployment and the private state container already used by mindMe.
New source reads are capped, model output capped, proposals bounded. Research
requires owner approval and names its scope; no recurring research is introduced.
Actual deployment identity, configuration and monthly budget remain release gates,
not claims made by offline tests.

## Verification

Synthetic tests cover near deadlines versus old undated items, changed old notes,
noise suppression, private/routed sources, section controls, source deletion,
corrections/snoozes, ambiguous and voice replies, duplicate approvals, persistence
failure, partial sends and the complete two-briefing feedback cycle.

Use independent diff review and the central leak audit before release. A live
Telegram cycle and subsequent briefing are required before claiming deployment
or real-world user acceptance.

Local verification on 2026-09-13: 453 targeted mindMe tests and 253 targeted memex
tests passed, including the shared persistence/writer consumers. The actual local
dashboard was parsed offline and all three generic approved priorities were
recognized without including its private-vault pointer. No live source refresh,
research job, task PR, Telegram send or deployment was performed.

That local checkpoint is superseded by the [verified release](deploy.md#verified-action-briefing-release-2026-09-13):
558/316 full-suite tests, deployed strict-schema validation, real Telegram delivery
and feedback reuse/deletion, and a synthetic idempotent task submission that was
closed without changing canonical task state. This does not measure long-term
usefulness or authorize new research/task work without a proposal decision.
