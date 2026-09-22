# Capture, understand, investigate, apply

## Approved outcome

Every article/video capture should provide an excellent source-grounded recap
without requiring the owner to consume the original. It should also be useful
after reading/watching as a refresher. Build all three approved improvements:
connected follow-up, governed continuity, and cross-source topic synthesis.
Evaluate the complete flow and correct observed failures before delivery.

## Existing foundations

- memex owns fetching, original evidence, compilation, capture receipts, the
  contextual Telegram summary, and routine guarded wiki publication.
- mindMe owns the Telegram webhook, private scoped state, safe canonical source
  reading, the approval-bound action loop, and daily/weekly reviews.
- Research already has a bounded public-question gateway. Do not create a
  second research executor, second knowledge vault, or unbounded chat history.
- The shared memory core is TypeScript; mindMe is an explicitly registered
  own-memory implementation. Extend its existing private state/validation and
  controls rather than silently installing an incompatible copy.

## Delivery slices

### 1. Substantial summaries and connected follow-up

The initial capture reply should explain the thesis, main ideas/reasoning,
important examples and caveats, followed by clearly separated interpretation and
one optional next step. Use adaptive depth: roughly 250-450 words for substantial
long content, shorter for short sources, never padding or invented examples.
All factual fields use the existing source-evidence validation. Telegram may
continue into complete messages; never silently truncate meaning. Disclose
transcript-only, metadata-only and bounded-input limitations.

The last summary message offers Explain more, Dig deeper and Apply this.
Replying to any summary part binds to that source. /recap URL requests a fresh
summary of an already captured source without another fetch or wiki PR.
Capture controls use `cap1|<action>|<first-32-source-id-hex>`.
Allowed actions: `explain`, `dig`, `apply`, `topic`, `known`, `useful`.
Every callback must match the actual delivered message and owner chat.

memex checkpoints `capture_reply_message_ids` with each confirmed Telegram send.
An existing source's recap may add message bindings, within a documented bound;
failed checkpointing is uncertain, never permission to resend automatically.

### 2. Governed continuity

mindMe loads a verified capture pointer, then the canonical source through the
existing safe reader. Explanation, comparisons and proposals are read-only.
Dig prepares a bounded research proposal; Apply prepares a modest task/experiment.
Only the existing explicit, source-version-bound approval engine can execute.
An incoming URL, a model suggestion, or a feedback button cannot grant approval.

Retain concise source-bound feedback, corrections, what was explained, topic
brief receipts and accepted/declined/action outcomes in private operational state.
No raw conversation archive, inferred permanent interests or private profile in
Git. Inspection, idempotent deletion, bounded size/retention, source invalidation,
supersession and recall-use recording ship together. Do not silently evict
unresolved action receipts. Repeated actions must not create duplicate work.

### 3. Topic synthesis and follow-through

Retrieve a small relevant set from allowed canonical knowledge, not just the
current task list. Rank deterministically before model synthesis; disclose
selection/size bounds and distinguish unavailable evidence from no matches.
Compare what sources add, agree on or challenge; cite actual source evidence.
Generated recaps/reviews are not independent corroboration of their originals.
Keep open questions, one optional next experiment and changed understanding in
inspectable, source-revision-bound topic briefs. Invalidate derived content when
a source changes, is deleted or becomes ineligible.

Use the existing morning/weekly work for bounded consolidation and follow-through
on accepted proposals. No new cron, automatic external browsing, repeated
unaccepted nudges or recursive research jobs. Routine captures retain their
existing automatic merge policy; this work does not broaden that merge authority.

## Cross-repository interface

`POST` to the existing Function-authenticated `dump/mindme` function accepts a
strict, read-only operation in addition to ordinary Telegram updates:

```json
{
  "operation": "capture_context",
  "version": 1,
  "chat_id": 7,
  "message_id": 123,
  "capture_key": "optional-32-lowercase-hex-callback-binding"
}
```

`capture_key` is omitted for a text reply. The owner must already be enrolled in
the mindMe bot/vault. Lookup matches persisted confirmed delivery message IDs,
not caller-supplied source content. Missing/ambiguous matches fail closed.

Response: `{"version":1,"status":"unmatched"}` or
`{"version":1,"status":"ready","source_id":"64-hex","source_path":"wiki/sources/name.md",
"source_url":"https://public-source.example/article","title":"Title"}`.
Only a confirmed publication receipt is eligible. The path is validated under
the configured source folder. No raw source, chat ID, token, or personal-work
context is returned. mindMe independently reads and validates the current
canonical source; pending merge/deleted/unsafe sources cannot authorize actions.
Read failures return explicit 503 and never fall through to an unrelated note.
No new app setting or function-key scope is needed.

## Acceptance matrix

| Requirement | Proof |
|---|---|
| Useful summary without consuming original | Multi-theme article and video evaluation: thesis, major ideas, examples/caveats retained; claims grounded; actual visible output reviewed |
| Honest short/incomplete sources | Sparse transcript and metadata-only fixtures never invent speech/visuals/details |
| Connected follow-up | Real webhook callback/reply resolves exact source, including split messages and edited updates |
| Research/action approval | Prepare performs no external mutation; explicit approval starts one existing bounded action; duplicate approval does not repeat |
| Memory has a purpose | Feedback affects later selection/replies; corrections supersede; actual recall updates usage |
| Memory is controlled | Inspection, idempotent forget, expiry, capacity and source deletion/invalidation tests |
| Topic synthesis is evidence-based | Relevant sources selected; citations resolve; conflict/gap/interpretation labels; derived-output loops excluded |
| Follow-through is truthful | Accepted actions retain status; submitted is not completed; no new cron or inferred activity |
| Existing behavior preserved | Existing capture, voice, daily review, proposal, allowlist, privacy and duplicate suites |
| Delivery | Independent review, scoped commits/PRs, checked merges, exact deployment and live verified outcomes |

## Budget and rollout

Reuse existing Functions, private storage, models and research infrastructure.
No dependency install or new Azure resource is expected. Bounded model calls are
on capture or explicit request; consolidation attaches to existing reviews.
Measure actual usage; stop for approval if estimated incremental cost exceeds
EUR 20/month or available credit is insufficient.

Deploy the backward-compatible memex bindings/context/recap first, then mindMe.
Keep old captures readable; /recap can provide new bound controls for old sources.
Record local tests, hosted CI, independent review, deployment and live acceptance
separately. Any incomplete delivery must be reported as incomplete.
