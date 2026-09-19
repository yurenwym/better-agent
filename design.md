# BetterAgent Design System

Approved direction: clear task workbench, from outputs/frontend-redesign-20260912/proposal.md.

## Structure

Modern-minimal application, not a marketing site. Workbench family for task pages;
continuous document layout for plans and research. Shared navigation, typography,
surface hierarchy and commands across all pages. No decorative hero or footer.

Primary navigation: conversation, today, goals, research. Personal memory and
review remain accessible. Engineering controls are grouped at the bottom.
Mobile uses one complete navigation drawer, including conversation history.

## Tokens

The existing frontend/src/tokens.css is the single token source. Preserve the
blue brand #2b4ac9. Light neutral sidebar, white content, restrained borders.
Body #202124, secondary #5f6368. Status foregrounds are distinct from backgrounds.
No card inside card. Sections are unframed; only repeated objects and dialogs
may use a single border, with 6-8px radius.

## Typography And Space

Preserve IBM Plex Sans with Chinese system fallbacks; JetBrains Mono for code.
Title 24/32, section 18/26, controls 14/20, prose 16/28, secondary 12/18.
Letter spacing 0. No viewport-dependent font sizes. Spacing 4/8/12/16/24/32.
Sidebar 240px, header 56px. Preserve reading width; never force four columns.

## Interaction

URLs restore selected objects and views. Keep legacy links functional.
Loading, empty, error and stale states are distinct. One primary action per
item. Preserve version checks, idempotency, CSRF and human approval boundaries.
No fabricated progress or undo for unsupported operations.
Native controls and existing React patterns first. Lucide for new tool icons.
Visible keyboard focus, 44px touch targets, drawer focus return and Escape.
Motion is limited to 140-220ms feedback and respects reduced motion.

## Scope

Implement the approved proposal incrementally. Do not replace business objects,
delete production modules or add unrelated frameworks. No automatic AI calls
for decorative summaries. Dark theme is deferred until both themes can be tested.
