import SwiftUI

/// The menu-bar status-item content: one compact readout per subscription —
/// the provider mark, the session percent left, and a micro fuel-bar showing
/// how much of that window remains. Monochrome (a single `tint`) because the
/// live status item renders to a *template* image that AppKit re-colors for
/// contrast over any wallpaper, so the number and the bar's length carry the
/// reading, never color. Severity color lives in the full card, one click away.
/// Offline collapses to three faint dashes.
public struct MenuBarContentView: View {
    public let snapshot: HUDSnapshot?
    public var now: Date
    /// The single monochrome color the whole glance draws in. The live status
    /// item renders this to a *template* image (so AppKit tints it for contrast
    /// over any wallpaper); the headless preview passes white to mimic that
    /// result on a dark strip.
    public var tint: Color

    public init(snapshot: HUDSnapshot?, now: Date = Date(), tint: Color = .primary) {
        self.snapshot = snapshot
        self.now = now
        self.tint = tint
    }

    // Fixed presentation order regardless of daemon ordering.
    private static let order = ["claude-team", "claude-personal", "codex"]

    private var orderedSubs: [Subscription] {
        guard let snap = snapshot else { return [] }
        return Self.order.compactMap { id in snap.subscriptions.first { $0.id == id } }
    }

    public var body: some View {
        HStack(spacing: 11) {
            if !orderedSubs.isEmpty {
                ForEach(orderedSubs) { sub in
                    HStack(spacing: 5) {
                        BrandMark(provider: sub.provider, size: 15, tint: tint)
                        GlanceMeter(window: sub.glanceWindow, tint: tint)
                    }
                }
            } else {
                ForEach(0..<3, id: \.self) { _ in
                    Text("–")
                        .font(Theme.mono(13, weight: .semibold))
                        .foregroundStyle(tint.opacity(0.35))
                }
            }
        }
        .padding(.horizontal, 5)
        .frame(height: 22)
    }
}

/// One plan's glance readout: the percent-left on top, and a fixed-width fuel
/// bar beneath whose fill is the fraction remaining — both drawn in the single
/// glance tint. A short bar means little left, so it agrees with its own number
/// at a glance without needing color.
struct GlanceMeter: View {
    let window: Window?
    let tint: Color

    private static let barWidth: CGFloat = 26
    private static let barHeight: CGFloat = 2

    var body: some View {
        VStack(alignment: .leading, spacing: 2.5) {
            Text(Fmt.glancePercent(pctLeft: window?.pctLeft))
                .font(Theme.mono(12, weight: .semibold))
                .foregroundStyle(tint)
                .monospacedDigit()
                .fixedSize()
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(tint.opacity(0.28))
                    .frame(width: Self.barWidth, height: Self.barHeight)
                Capsule()
                    .fill(tint)
                    .frame(
                        width: Self.barWidth * Fmt.remaining(pctLeft: window?.pctLeft),
                        height: Self.barHeight
                    )
            }
        }
    }
}
