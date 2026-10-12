# Task workspace interface

The web surface is an operating tool for one personal owner, not a marketing page.
The implemented source is `harness/web/index.html`, `tasks.css`, and `tasks.js`.

## Composition

A short navigation rail leads to Today, Tasks, Knowledge, Areas & projects, and
Activity. Needs you and All tasks remain filters inside Tasks.
The task ledger and a detail pane share the desktop work surface. On narrow screens,
opening a task replaces the list; a visible Back to tasks control restores it.
The optional board is horizontally scrollable, with normal forms for stage changes.
Dragging is never required.

Today puts dated approved focus before a paired view of task attention and source
changes. Knowledge starts with an Inbox, using the same ruled ledger and detail
pane to show exact quotations and a same-origin source reader. A finding's
evidence takes one interaction; its source takes two. On a phone the reader
replaces the inbox with an explicit Back control. No future-phase tabs are shown.

Capture remains in the header. Exact proposed changes are reviewed in the work
stream, not behind a generic confirmation modal. The interface distinguishes
drafting, approval, publication, canonical updates, and real-world completion.

## Visual system

Use the system UI font stack, with fixed rem sizes and restrained weight changes.
The page ground is `#f4f7f6`, work surfaces are white, and primary text is `#20352f`.
Secondary text is `#53675f`; the action/selection accent is `#006b53`.
Amber means waiting or pending, not failure. Red marks errors or passed deadlines.
Color is accompanied by a text label.

Flat ruled rows carry tasks. Borders separate responsibilities; do not add decorative
metrics, gradients, glass, or nested card grids. Controls have an 8px radius and
at least 44px primary touch targets. Only color/border state transitions animate,
and reduced-motion preferences are respected.

## Behavior and accessibility

- Keep native buttons, fields, details, labels, visible focus and keyboard navigation.
- Show an unavailable source as unavailable, never as a zero count or empty backlog.
- Display the complete exact action before its approval button.
- Do not render source or model text as HTML; use DOM text nodes.
- Clear task content and unsaved searches at sign-out or session expiration.
- Keep an unconfirmed request's original ID and payload for explicit recovery.
- Retain server receipt history, not browser local/session storage or offline task caches.
- Pages are bounded; counts and filters explicitly refer to loaded records.
- Redacted history cannot reveal a former action or preparation payload.
- No external fonts, scripts, analytics, or image services are used.

The responsive boundaries are 1160px, 930px and 690px. Changes should preserve the
390px phone and 1280px desktop layouts, source-state clarity, and readable forms
before introducing additional visual effects.
