# Acceptance Criteria - Link Dialog Copy Control

The Share-link dialog offers the link again on demand - the auto-copy at creation is lost as soon as anything else hits the clipboard. The copy control is an icon embedded inside the link input, not a separate button.

## Design language

Browser-URL-bar idiom: the action lives inside the field it acts on. One continuous control - the input supplies the only border and background; the icon carries no chrome of its own (no border, no fill, no button shape), so the row reads as a single field with an affordance, not a field plus a button. State changes are communicated in place by swapping the glyph, never by growing the control or adding text. All colors come from JupyterLab theme variables so the control blends in both light and dark themes:

- **Resting** - 16px JupyterLab `copyIcon`, `jp-icon3` grey (`--jp-ui-font-color2` tone), right edge of the input, vertically centered
- **Hover** - subtle `--jp-layout-color2` rounded square behind the icon (toolbar-button hover), cursor pointer, tooltip "Copy link"
- **Success** - glyph swaps to `checkIcon` filled `--jp-success-color1` for 1.2 s, then reverts
- **Failure** - copy glyph refilled `--jp-error-color1` for 1.2 s, then reverts
- **Contrast with dialog actions** - the primary dialog action (Close) keeps the standard solid `jp-mod-styled` look; inline helpers inside content never compete with it

The password Copy button (small bordered text button on the password status line) is intentionally different - it sits on a status row, not inside a field, and the user approved its look; it stays as-is.

## Criteria

- [x] **Embedded placement** - copy icon renders inside the link input at its right edge; input text gets right padding (34px) so the link never runs under the icon
  - log: 2026-06-12 implemented, verified via Playwright bounding boxes (icon box inside input box)
- [x] **No button chrome** - transparent background, no border; hover shows a `--jp-layout-color2` highlight only
  - log: 2026-06-12 implemented
- [x] **Copy action** - click copies the full link via `_copyLinkToClipboard` (Clipboard API, `execCommand` fallback on http origins)
  - log: 2026-06-12 implemented
- [x] **Success feedback** - glyph flips to a green check for 1.2 s, then back; no text, no resize
  - log: 2026-06-12 implemented, fill sampled live: grey -> `--jp-success-color1` -> grey
- [x] **Failure feedback** - copy glyph flashes `--jp-error-color1` for 1.2 s
  - log: 2026-06-12 implemented
- [x] **jp-mod-styled override** - styled via `.jp-Dialog-content button.jp-ShareFiles-copyEmbed` in `style/base.css` so the dialog's auto-applied `jp-mod-styled` (32px line-height, min-width, background) cannot inflate it
  - log: 2026-06-12 implemented, same specificity trick as `.jp-ShareFiles-miniBtn`
- [x] **Layout intact** - password line, copied-status, reachability and QR keep their order below the link row (password inserts at `linkRow.nextSibling`)
  - log: 2026-06-12 implemented
- [x] **Edge: theme switch** - all colors via theme variables; correct in light and dark
  - log: 2026-06-12 implemented, dark theme verified via screenshot
- [x] **Edge: focus-select unaffected** - clicking the input still selects the full link; the icon click does not steal the selection behaviour
  - log: 2026-06-12 implemented

## History

- 2026-06-12 v1.2.33 shipped a detached grey text button ("Copy" chip next to the input) - rejected as wrong design
- 2026-06-12 redesigned to the embedded-icon idiom above
