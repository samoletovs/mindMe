# ADR-0001 — Knowledge storage architecture & capture command surface

> **Status:** Accepted — 2026-07-15
> **Scope:** mindMe (front door, briefing, companion) + memex (capture engine, stores)
> **Supersedes:** nothing. First ADR in this repo.

## Context

The personal knowledge system currently works like this:

- **Capture is one undifferentiated intent.** `save:` / `note:` / `idea:` / `n:`
  are synonyms — the prefix is stripped and discarded, and the note *type* is
  chosen by the LLM classifier (`memex/functions/capture.py`), not by the word.
  A bare URL (incl. YouTube) and voice notes are auto-captured with no prefix.
- **Two data stores, cleanly split by sensitivity:**
  - **mindVault** (`samoletovs/mindVault`, markdown-in-git) — notes, `/dig`
    research reports, the compounding wiki. Non-sensitive, PR-reviewed.
  - **`.me`** (OneDrive, never git) — the sensitive personal OS. A private
    `personal-os` blob mirror of it powers the morning briefing, but the
    briefing only ever emits **sanitized aggregates** (counts, dates, ages).
- **The companion has no retrieval.** Its only tools are `get_briefing_context`
  and `get_weather` (`mindMe/agent/openapi-tools.json`). There is no
  "search my vault" capability today.

Two questions were raised:

1. Should capture split into distinct flows — `/note` (knowledge), `/idea`
   (follow-up), `/task` (actionable) — plus a conversational "ask my vault" mode?
2. Is markdown-in-git the right place to store all this, or should it move to a
   database / RAG / Cosmos DB / Azure AI Search?

## Decision

### D1 — Markdown-in-git stays the system of record; add derived read models

`mindVault` markdown-in-git remains the **canonical source of truth**. It wins
for a single-user, lifelong, sensitivity-tiered, agent-*and*-human-edited store:
ownership & longevity (no lock-in), git history + **PR-review-before-merge** on
agent writes, a sensitivity model that *depends on* files-in-repos
(`.gitignore`, separate repos, `*.private.md`), and near-zero cost.

Its weaknesses (semantic retrieval, structured/aggregate queries, per-request
latency) are solved by **derived read models built *from* the markdown**, never
by moving the truth into a database:

- a **structured state projection** (open ideas, tasks, deadlines) for the
  briefing / resurfacing / "list my open tasks";
- an **embeddings index** for semantic conversation (RAG).

Derived stores are **disposable and rebuildable from git** — if any index dies,
re-index from the repo. They are caches, never truth.

### D2 — Store choices (reuse what we already run; €10/mo budget)

- **Reuse Cosmos DB** (already deployed for memex dedup) for the state
  projection and vectors (Cosmos has native vector search).
- **Reuse Azure OpenAI** (already wired for chat) — add an **embeddings
  deployment**; no new service.
- **Defer Azure AI Search** until deterministic + Cosmos-vector retrieval
  visibly falls short (hybrid keyword+vector, ranking, large corpus).
- **Nightly rebuild** of projection + embeddings from git (personal scale — a
  full rebuild is simpler and more robust than incremental).

### D3 — Capture command surface

Three specific verbs (Telegram guidance: *"commands should be as specific as
possible"*). **The command sets the lifecycle; the LLM still shapes the content.**

| Command | GTD end-point | Home | Lifecycle |
|---|---|---|---|
| `/note` | reference | mindVault `notes/` | stored knowledge, retrieved as context |
| `/idea` | someday/maybe | mindVault `ideas/`, `status: open` | stored **and resurfaced** |
| `/task` | next action / project | mindVault `tasks/` ~~GitHub issue~~ | actionable, subtasks; presence = open |

> **Superseded 2026-08-04 (D4.1 only)** — `/task` writes markdown to `tasks/`, not a GitHub
> issue. See [mindVault DR-003](https://github.com/samoletovs/mindVault/blob/main/decisions/DR-003-tasks-as-markdown.md).
> The rest of this ADR stands.

Open loops are delivered to the briefing by a memex `/state?vault=…` endpoint
(read-only over the vault repo — ideas from the `ideas/` folder, tasks from the
`tasks/` folder) that mindMe fetches via `MEMEX_STATE_URL`; unset →
resurfacing silently disabled. Never reads `.me` (D5).

- Keep the `n:` / `note:` / `idea:` prefixes (muscle memory / speed), and keep
  **bare URL + voice auto-capture** (no command required — never regress those).
- **`/save` is dropped** (redundant synonym).
- Register the set via `setMyCommands` for the `/` menu.

### D4 — Fork resolutions

1. ~~**Tasks → GitHub issues** in mindVault (reuses the `/dig` machinery; issues
   give subtask checklists + reminders). Personal vault only.~~ **Superseded
   2026-08-04 by [mindVault DR-003](https://github.com/samoletovs/mindVault/blob/main/decisions/DR-003-tasks-as-markdown.md):**
   tasks are markdown in `tasks/`. Issues were invisible from the vault, accumulated
   near-duplicates, and their open/closed lifecycle silently failed (the reapers'
   `GITHUB_TOKEN` lacked `issues: write`). Issues remain for **agent work orders**
   (`dig`, `dispatch`, `promote`, newsletters), where the issue *is* the trigger.
2. **Idea resurfacing → morning briefing *and* weekly review** (GTD "reflect":
   surface N oldest un-reviewed open ideas).
3. **Retrieval → deterministic first** (newest-file / frontmatter queries via
   the state projection), **embeddings second** for semantic Q&A.

### D5 — Sensitivity boundary (fail closed)

Conversational retrieval and every derived index cover **mindVault only, never
`.me`**. The `.me` mirror stays sanitized-aggregates-only. Enforced in code
(repo + path allowlist), not in the prompt.

## Grounding

- **GTD** (clarify fork: reference / someday-maybe / next-action-or-project;
  weekly review) — <https://en.wikipedia.org/wiki/Getting_Things_Done>
- **PARA** (organize by actionability) — <https://fortelabs.com/blog/para/>
- **Telegram Bot Features** (specific commands, `setMyCommands`, menu) —
  <https://core.telegram.org/bots/features>

## Consequences

- **Positive:** ownership, PR-reviewed agent writes, legible sensitivity tiers,
  low cost; features (`/idea` resurfacing, vault Q&A) ride existing seams.
- **Negative / cost:** a **sync seam** between markdown and the derived indexes
  (a place for staleness). Mitigated by rebuildable-from-git + nightly rebuild.
- **Revisit if:** multi-user concurrent editing, transactional/relational needs,
  or ~10k+ notes where hand-editing stops mattering — then reconsider the
  DB-as-truth calculus.

## Phased plan

| Phase | Work | Touches | Deploy/Azure? |
|---|---|---|---|
| **P1** | `/note` + `/idea` capture verbs; `idea` gets `status: open` | memex, mindMe | no (code + tests) |
| **P1b** | `/task` → markdown task in `tasks/` with LLM-expanded subtasks (was: labelled GitHub issue — DR-003) | memex | no (code) |
| **P2** | State projection (ideas/tasks/deadlines), rebuildable | memex/mindMe | later |
| **P3** | Resurface open ideas/tasks in briefing + weekly review | mindMe | deploy |
| **P4** | Conversational retrieval tools `get_vault_recent` / `get_vault_read`, mindVault-scoped, **deterministic** (embeddings deferred) | mindMe + Foundry | deploy + Azure |
| **P5** | `setMyCommands` registration (`scripts/dev/set_bot_commands.py`); **nightly rebuild deferred** — nothing to rebuild while `/state` is on-demand and P4 is deterministic; revisit when embeddings land | both | deploy + Azure |

Anything under **deploy/Azure** requires explicit go-ahead before running.

> **Deferred by design:** the **embeddings/vector tier** (D2) and the **nightly
> rebuild** (P5) are not built yet — deterministic retrieval + on-demand `/state`
> cover current needs at zero standing cost. Add them together when semantic
> "what did I think about X" search is wanted.
