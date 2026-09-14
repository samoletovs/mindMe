# Daily mindVault knowledge review

This is an executable cloud adapter, not a claim that an Azure Function can load
editor skills. Its reasoning and evidence format implement the shared
`vault-evolve` **receipt v1**, reviewed at shared-toolchain revision
`995257cdc183c8c12a15ee43b3c210db6da7a0d9` (2026-09-13).
The canonical skill remains in the shared skills library; no corporate repository
credential or customer source is required by this personal runtime.

When changing that shared contract, explicitly review this adapter and the memex
writer together. Keep the synthetic quote-ID, connection, proposal-only and
readable-pair tests green. Do not silently fork the contract or describe integrity
checking as proof of factual correctness or knowledge acquisition.

## Runtime and boundaries

- `MINDME_DAILY_EVOLVE_ENABLED=true` opts in to the existing **07:30 UTC** morning
  timer. There is no new cron, workflow, model deployment or infrastructure SKU.
- The saved `knowledge` briefing-section preference controls the scheduled review.
  `/briefing off knowledge` pauses it; `/briefing on knowledge` resumes it.
  Explicit `/evolve now` is an on-demand request even while that section is off.
- The original briefing and the knowledge review are independent deliveries.
  Failure is surfaced, not treated as a successful empty review; one failing
  review does not prevent the original briefing.
- The host timeout is five minutes for the combined timer workload, within the
  existing consumption plan. An HTTP caller can time out earlier; inspect retained
  state before retrying rather than inferring failure or success from that timeout.
- Only the configured personal **mindVault** repository is permitted. Work vaults,
  the private mirror, raw/ignored material and generated reviews are excluded.
  Source selection and approved focus extraction reuse the existing safe reader.
- At most 16 source fetches, 12 evidence sources, 9,000 quotation characters and
  three findings per review. Model output is capped at 2,400 tokens on the existing
  configured briefing model; at most two generation attempts per UTC day.
  Review files are capped at 64,000 bytes each, 512,000 bytes total and six path
  components, matching the writer without narrowing legacy briefing reads.
- The model selects supplied source/quotation IDs. Code restores the actual
  literal quotations, raw-byte SHA-256 hashes and proposal-only receipt.
  Source revisions are rechecked before publication. memex independently checks
  source bytes, quotations and source eligibility before accepting the pair.
- New observations, evidence gaps, conceptual bridges and application candidates
  are interpretations of a bounded selection, not a diagnosis of the owner's
  knowledge. Repeated summaries are not independent corroboration.

## Persistence, publication and approval

The existing private container holds
`system/mindme/vault-evolve-state-v1.json`: up to 14 daily records, source manifests,
review/delivery receipts and scoped feedback. It uses the existing 1 MiB CAS
store at a separate blob path; action-briefing state is not repurposed.

Findings produce immutable `review.md` and `review.json` files under
`reviews/vault-evolve/YYYY-MM-DD/` through the existing authenticated memex
`personal_action` writer. A submitted PR is described as **not yet canonical**;
neither submission nor Telegram delivery means its proposals are approved.
Quiet/no-action reviews remain private and do not create empty vault PRs.
Published reviews are derived artifacts, never sources for the next review.

One date-bound action ID is reused after an uncertain write; prepared content is
not regenerated or substituted. The canonical commit pin remains outside the
exact v1 receipt in the writer request envelope and is reused unchanged. A CAS
claim prevents simultaneous preparation.
Message IDs are checkpointed after each confirmed Telegram send. An interrupted
send with unknown outcome blocks automatic retry: `/evolve retry` explicitly
accepts that the last message may be duplicated. Completed daily runs are no-ops.

No review button starts research, creates tasks, edits wiki conclusions, or marks
material read/understood. Use the existing `/dig <public question>` or
`/task <exact task>` for an explicit new action. Curation remains a reviewed edit.
The existing `brief1|` proposal approvals and memex capture callbacks are unchanged.

## Telegram controls and feedback

| Command/interaction | Meaning |
|---|---|
| `/evolve` | Show today's review and its recorded publication receipt |
| `/evolve now` | Prepare/deliver today if not already completed |
| `/evolve retry` | Explicitly retry an unconfirmed delivery; last message may repeat |
| Useful / Already know / Not useful | Save feedback bound to the actual message/finding |
| Reply with text or voice | Save at most 280 non-sensitive characters of scoped feedback |
| Reply `why` | Show literal supporting quotations |
| Reply `snooze YYYY-MM-DD` | Reconsider within the 13-day feedback horizon |
| `/evolve feedback` | Inspect retained feedback |
| `/evolve forget YYYY-MM-DD` | Remove that day's feedback, not a published artifact |

Feedback expires with the 14-day review window and is not a new permanent profile.
Deletion removes it from future model inputs; it is not retained in cached prompts.
A changed/deleted or newly excluded source invalidates retained derived text on
the next daily scan or relevant interactive read. Delivery retries recheck sources
and cannot overwrite a concurrent invalidation. Expiry applies on access too,
not just when the timer runs; accepted snoozes cannot outlive their review record.
Only confirmed delivered findings suppress repetition, identified by source
versions, finding kind and exact quoted evidence rather than model wording.
Distinct evidence from the same page remains eligible for a new finding.

The writer uses an immutable per-day PR, not an accumulating mutable review branch.
Unmerged review PRs can therefore accumulate; quiet days do not add one. A branch-only
uncertain write requires operator reconciliation instead of blind retry or revival.

## Release and rollback

Deploy the memex writer first; deploy this runtime with its flag off; verify
existing capture, owner auth and `/ping`. Enable only after the writer and current
model work. Verify `/evolve now` against real allowed sources and actual Telegram
message receipts plus the paired artifact PR. Run again to prove daily idempotence.
Health HTTP 200 alone is insufficient.

Rollback: set `MINDME_DAILY_EVOLVE_ENABLED=false` without deleting receipts or
changing the original action-briefing flag. The Bicep template default is off;
the approved personal production parameter is on. Use the existing safe
configuration-update procedure, not a full clean-rebuild deployment.
