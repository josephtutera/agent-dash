# AgentDashHUD

A native macOS HUD (Phases 3 and 4 of the agent-dash HUD program) that renders
the live state of your AI subscriptions and running coding agents. It is the
on-screen face for the `adash serve` daemon: a dark instrument-cluster readout
of how much of each limit you have left, which agents need you, and what all
that usage would cost at API rates.

It wears one of two faces, decided automatically from the display arrangement:

- **Notch mode** — when the built-in display is the only display and has a
  notch, a black pill hugs the camera housing: one 26pt concentric ring cluster
  per subscription on the left (outer ring 5h session, middle weekly, inner
  Fable; Codex has two rings), the soonest-reset countdown on the right, and
  cluster brightness carrying the agents-running signal. Hovering the pill
  hangs the full detail card below the notch; moving away collapses it.
- **Menubar mode** — whenever an external display is attached (or the lid is
  closed, or the built-in panel has no notch), the pill gives way to an
  NSStatusItem with 18pt two-ring mini clusters (inner ring = the tightest
  weekly) plus the countdown; clicking opens the same card as a panel. A fake
  notch is never drawn on an external display.

The switch re-evaluates on every display-configuration change, rebuilding the
notch window rather than moving it. The notch panel deliberately stays out of
fullscreen spaces (no `.fullScreenAuxiliary`), and its window is sized exactly
to the drawn pill so it never steals clicks meant for menubar items.

This is a pure SwiftPM package with no third-party dependencies. macOS 14+,
Swift 5.9+.

## Build and run

```sh
cd hud
swift build -c release          # build
swift run adash-hud             # launch the menubar app (no dock icon)
```

The app runs with `.accessory` activation policy, so it lives only in the
menubar with no dock icon or main window. The menubar item shows three mini
concentric ring clusters (Claude Team, Claude Personal, Codex, in that order)
and the soonest-reset countdown in amber. Clicking it opens the detail card.

To quit, use Activity Monitor or `pkill adash-hud` for now; a quit affordance is
a later polish item.

## How it finds the daemon

Every 2 seconds the app fetches `http://127.0.0.1:8737/v1/hud` over HTTP with a
tight 1.5s timeout. The response is the `version: 1` snapshot contract (limits,
agents, value, soonest reset). If that request fails, it falls back to reading
the same JSON from `~/.cache/adash/hud.json`, which the daemon also writes. If
both fail, the menubar collapses to three dim gray rings with no countdown and
the card shows `daemon offline, run: adash serve`. A `stale` field on any
subscription dims that section's numbers and appends the reason to its header.

Countdowns re-render every 30 seconds off the `resets_at` timestamps, so time
keeps moving even between daemon polls.

## Preview render (the review artifact)

The card view can be rasterized headlessly to a PNG with SwiftUI's
`ImageRenderer`, using a committed fixture snapshot, so reviewers can see the UI
without running the menubar:

```sh
swift run adash-hud --render-preview preview.png
swift run adash-hud --render-preview-notch preview-notch.png
```

The committed `preview.png` (the card) and `preview-notch.png` (the notch pill,
collapsed and hover-expanded) are generated this way. The same render paths are
exercised by `RenderTests` and `NotchTests` as layout smoke tests.

## Tests

```sh
swift test
```

Covers JSON decoding of the committed fixture (`Tests/HUDCoreTests/Fixtures/snapshot.json`),
the countdown-format ladder and its edge cases, severity color mapping,
consumed-fraction math, cluster-opacity derivations, and a headless render smoke
test.

## Architecture

- `HUDCore` (library): the Codable contract structs, the theme tokens, the pure
  formatting helpers, the SwiftUI views (ring cluster, notch face, brand marks,
  popover card), the display-mode policy, the polling `HUDStore`, and the
  preview renderer. Everything testable lives here.
- `adash-hud` (executable): a thin AppKit shell. It applies `ModePolicy` on
  launch and on display changes, wiring either the `NSStatusItem` +
  click-to-open panel (menubar mode) or the `NotchController`'s hover-expanding
  pill panel (notch mode), and handles the `--render-preview*` flags.

## Still to come (Phase 5 polish)

The row click-to-focus action (jump to that agent's Warp tab), the real
"open agent dash" target (currently an `open -a Warp` placeholder), a quit
affordance, and expand/collapse animation on the notch pill.
