# AgentDashHUD

A native macOS menubar app (Phase 3 of the agent-dash HUD program) that renders
the live state of your AI subscriptions and running coding agents. It is the
menubar face for the `adash serve` daemon: a dark instrument-cluster readout of
how much of each limit you have left, which agents need you, and what all that
usage would cost at API rates.

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
```

The committed `preview.png` is generated this way. The same render path is
exercised by `RenderTests` as a layout smoke test.

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
  formatting helpers, the SwiftUI views (ring cluster, brand marks, popover
  card), the polling `HUDStore`, and the preview renderer. Everything testable
  lives here.
- `adash-hud` (executable): a thin AppKit shell. It wires an `NSStatusItem`
  hosting the ring-cluster view to a non-activating `NSPanel` that shows the
  card on click, and handles the `--render-preview` flag.

## Notch mode is Phase 4

This phase is the menubar item plus click-to-open card. The notch-hugging
always-on display, the row click-to-focus action, and the real "open agent dash"
target (currently a `open -a Warp` placeholder) all land in Phase 4.
