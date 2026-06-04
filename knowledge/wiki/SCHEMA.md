# mindMe Knowledge Wiki — Schema & Maintenance Protocol

> **What this folder is.** Sam's personal operating system — a persistent,
> compounding knowledge layer about Sam's own life: health, decisions, books,
> goals, reflections, life trends. It is the **personal** counterpart to
> NauroLabs' lab wiki.
>
> **Where new entries come from.** Sam drops URLs into a Telegram chat. The
> [memex](https://github.com/samoletovs/memex) engine fetches, extracts, and
> compiles each source into wiki edits via a pull request to this repo.
>
> **Who reads it.** Only Sam (and his agents). It is not published.

## Voice

First-person, reflective. Use **I / my** when summarizing Sam's own decisions,
goals, and reflections; third-person for external articles, books, and tools.
Connect every entry back to a long-running goal or open question when relevant.

Skip jargon that Sam doesn't already use day-to-day. The wiki should sound like
Sam's own notebook, not a corporate report.

## What goes here

| Kind | Examples | Folder |
|------|----------|--------|
| **Sources** | Raw per-URL pages — one per ingested link | `sources/` |
| **Entities** | Stable people, places, books, tools, recurring concepts | `entities/` |
| **Insights** | Things I've learned that recur across multiple sources | `insights/` |
| **Trends** | External-world shifts I want to track (health science, AI, markets) | `trends/` |

If a source is a one-off curiosity, it stops at `sources/`. Only promote into
`entities/`, `insights/`, or `trends/` when the topic recurs (2+ sources or
2+ ingest events).

## Structure

```
knowledge/wiki/
├── SCHEMA.md           ← this file
├── index.md            ← catalog of every wiki page
├── log.md              ← append-only chronological log of ingests / lints
├── sources/            ← one .md per ingested URL (memex writes these)
├── entities/           ← stable cross-references
├── insights/           ← personal patterns I've noticed
└── trends/             ← external-world shifts I'm tracking
```

## Page template

Every page starts with a small standard block so I can scan freshness at a glance.

```markdown
# {Page Title}

**Status:** active | provisional | stale | superseded-by-{other-page}
**Last verified:** YYYY-MM-DD
**First filed:** YYYY-MM-DD
**Sources:**
- `sources/<source_id>.md` — original drop
- `https://example.com/article` — external reference
**Tags:** health, sleep, decisions  *(lowercase, kebab-case)*

## TL;DR
One paragraph I can read in 20 seconds.

## Body
...

## Open questions
- ...
```

**Status values**
- **active** — current, verified within the last 90 days
- **provisional** — first observation, not yet confirmed
- **stale** — older than 90 days without re-confirmation; lint flags these
- **superseded-by-{path}** — kept for history, links to replacement

## Source page template

Every URL drop becomes a `sources/<source_id>.md` with this shape:

```markdown
# {Article title}

**Status:** active
**First filed:** YYYY-MM-DD
**URL:** {original url}
**Adapter:** web | youtube | reddit | hackernews | podcast | sharepoint
**Tags:** tag1, tag2

## TL;DR
3-5 sentences capturing what the source actually says, in my voice.

## Highlights
- key point one
- key point two
- verbatim quote with attribution

## Why I filed this
One sentence — what hooked me, why this matters to me right now.

## Links
- entities/people/...
- insights/...
```

## The three operations

### Ingest
1. memex reads `index.md` to know what already exists.
2. Creates `sources/<source_id>.md` for every URL.
3. Updates / creates `entities`, `insights`, `trends` only when the source
   adds something new or contradicts existing content. Contradictions are
   flagged with `> ⚠️ Contradiction noted YYYY-MM-DD`, never silently
   overwritten.
4. Updates `index.md`.
5. Appends one line to `log.md`:
   ```
   ## [YYYY-MM-DD] ingest | <source title> → updated insights/sleep-protocol.md
   ```

### Query
1. Read `index.md` first.
2. Drill into pages. Cite by markdown link.
3. If the answer is a useful new synthesis, file it back as a page or update an existing one.

### Lint (monthly)
- Stale pages (>90 days) → re-verify or mark stale.
- Unresolved contradiction blocks → resolve or graduate to open questions.
- Orphan pages (zero inbound links).
- Coverage gaps (same topic in 3+ sources without an insight page).
- Index drift (every file ↔ every entry).

## Scope discipline

- ❌ Health metrics raw data → goes to whatever app I use, not here. Only insights and decisions belong.
- ❌ Random links I never re-read → stop them at `sources/`, never promote.
- ❌ Lab knowledge (NauroLabs strategy, infra, projects) → lives in the
      [NauroLabs wiki](https://github.com/samoletovs/nauroLabs-github/tree/master/.github/wiki).
- ❌ Family / household knowledge → lives in
      [agentMode/knowledge/wiki](https://github.com/samoletovs/agentMode/tree/main/knowledge/wiki).
- ❌ How-to guides for tools → keep in tool's own docs, link to them.

## Why this exists

Inspired by [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).
Sam's personal knowledge is fragmented across notes apps, Telegram saves,
podcasts, and bookmarks. The wiki is the synthesis layer that compounds
across years.
