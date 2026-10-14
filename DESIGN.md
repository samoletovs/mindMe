# Vault kinetic studio

The owner's personal working space, not a marketing dashboard or a second task
store. The selected direction replaces both the old green ledger and the
violet/gradient workspace. Source: `harness/web/index.html`, `tasks.css`,
`tasks.js`, and `dashboard.js`.

## Identity

Graphite, warm ivory, and a narrow electric-chartreuse signal. Open space, aligned
rules, expressive display type and readable working text carry the identity.
The default is dark even when the operating system prefers light; the header
offers an equally complete light theme. Theme and language choices stay in memory.

| Role | Dark | Light |
|---|---|---|
| Canvas | `#171916` | `#f0eee5` |
| Work surface | `#1e211d` | `#faf9f3` |
| Primary text | `#f0eee5` | `#24291e` |
| Secondary text | `#b0b4a7` | `#5a6250` |
| Selection / links | `#d5f879` | `#466517` |
| Primary action | `#d5f879` with `#202719` text | Same |

Warm amber denotes waiting or pending; muted coral denotes failures, invalidation
and overdue dates. Every status has a text label. Work controls use a 5px radius;
the copilot and phone action bar use 12px. Most surfaces are flat: shadows only
separate an overlapping drawer, dialog, tooltip or toast.

Display typography uses native Helvetica Neue / Arial; working text uses the
system UI stack. Both support Cyrillic without a font download. Large type is
reserved for sign-in and the current view; task/source prose remains compact and
readable. No external fonts, images, scripts, analytics or frontend dependencies.

## Composition

- **Sign-in:** oversized two-line type beside an original self-contained SVG
  folding study. The graphic is decorative, not a knowledge map, progress signal
  or claim about personal data. Sign-in, auth failure and retry remain real BFF
  states; no demonstration identity is used on the deployed origin.
- **Today:** a compact next-move heading, a ranked ruled list of up to three
  server-eligible loaded tasks, and a separate approved-focus panel. Source dates,
  expired/draft focus and partial coverage are explicit. Recorded estimates name
  how many of the shown tasks have estimates; they are not an invented total.
  Task attention and source changes follow below, without a decorative hero
  taking over the working viewport. One shared ranking supplies the priority
  list; each task appears once. Priority entries carry their exact dates, stage,
  dependencies and definition warnings. The secondary section shows additional
  loaded attention, waiting context, decisions and results, with full Flow access.
- **Flow:** a scrollable six-lane board or a dense ruled list, real stage labels,
  filters, outcome/done conditions, a seven-step preparation rail and task chat.
  Stage changes have normal keyboard-accessible forms; dragging is optional.
  Between 1101 and 1599px, the board's task drawer starts below the measured
  header. At 1100px and below, the selected detail replaces the task list.
- **Knowledge:** a finding inbox and an evidence reader. Exact quotations lead
  to pinned same-origin source reads. The source reader's optional scroll-linked
  line is a position indicator, never a saved reading or completion receipt.
  On phones, the reader replaces the inbox with a clear return control.
- **Areas, Activity and Capture:** the same rules, typography and control grammar.
  Counts describe loaded permitted records. Proposals retain exact action
  previews, publication receipts and the distinction between preparation and
  verified real-world completion.

The navigation rail becomes a horizontally scrollable strip at 930px. Desktop
header actions remain available at 1024px and above. Below 1024px, the phone/tablet
action bar supplies capture, voice and copilot with reserved bottom/safe-area
space. Refresh and sign-out stay reachable on phones. The layout also covers a
320px narrow viewport; long EN/RU text must wrap rather than widen the page.
Header actions and form groups also reflow at 200% text resizing. The phone bar's
measured height reserves its bottom space; text is not reduced to fit. Operational
dates, statuses and metadata share a 0.75rem minimum role and scale with text size.

## Interaction

One finite sign-in entrance and line reveal establish the folding motif. Navigation
transitions only the work area; chrome stays fixed. In-flight transitions are
skipped, the old snapshot is invisible, and only one view panel can remain visible.
Drawers/dialogs have short entrance choreography. Buttons respond to pressure;
task movement animates only a confirmed rendered position change.

No ambient orbit, endless loading shimmer or synthetic progress. The voice
waveform animates only during actual recording and is explicitly decorative;
recording/transcribing/error status is also readable text. Reduced motion removes
all CSS and task-position animation. Unsupported view/scroll-transition APIs
fall back to normal navigation and reading; content is never animation-dependent.

Native controls, visible focus and 44px primary targets remain the baseline.
Navigation supports arrow/Home/End keys. Task and reader opening moves focus;
Escape/back restores an available trigger. Capture returns to its actual entry
point, including the phone bar. Tooltips are touch/keyboard reachable and
Escape-dismissable. Voice/API failures are visible inside the modal copilot as
well as in the workspace, not only behind its backdrop.

Background receipt refresh does not reset the selected tab or a same-source
composer while reading. Manual edit/date/preparation/closure/clarification drafts
are bounded to the selected task's exact path and revision, in memory only.
Locale changes and same-source rerenders retain input, selection and caret;
clarification answers additionally require the same question token. A changed
or unavailable source visibly discards incompatible manual drafts, never rebases
them. Explicit close/new selection and sign-out/expiry clear them.
Draft/caret/scroll restoration is selection- and focus-aware.
A changed/removed revision still invalidates old source content;
only a still-permitted, freshly re-read task can restore the selected view.
Explicit close/navigation, a withheld source or a denied re-read cannot reopen it.
Deliberate navigation establishes heading or selected-detail context; locale-only
rendering and background refresh do not perform navigation. Initial loading does
not move focus or scroll. On narrow screens, ordinary view arrival positions the
navigation strip below the header with the new view context, rather than hiding
the strip above the heading. Task/evidence drill-down keeps its own positioning.
Normal signed-out states show Sign in alone; retry is for genuine service/network
recovery.

Source/model strings remain DOM text, never HTML. Requests, UUID formats, approval
digests, receipt polling, Undo eligibility and closure gates remain service-owned.
Sign-out/expiry clears private UI state. There is no browser task or credential
storage and no new inference call.

## Public references

These informed the design, not a copied template or artwork:

- [Linear's UI redesign](https://linear.app/now/how-we-redesigned-the-linear-ui):
  aligned navigation/header/panels, reduced chrome, testing real list/split views.
- [Craft](https://rauno.me/craft) and
  [Awwwards animation collection](https://www.awwwards.com/websites/animation/):
  authored hierarchy and purposeful interaction detail rather than decoration.
- [Same-document view transitions](https://developer.chrome.com/docs/web-platform/view-transitions):
  fixed chrome, scoped progressive enhancement and explicit snapshot handling.
- [Scroll-driven animations](https://developer.chrome.com/docs/css-ui/scroll-driven-animations):
  a compositor-driven source-reader position line, with reduced-motion fallback.

Synthetic browser fixtures are labelled and loopback-only. Screenshots belong in
session artifacts, not the repository. See `docs/task-workspace.md` for the
runtime/safety contract and `docs/vault-dashboard.md` for the evidence reader.
