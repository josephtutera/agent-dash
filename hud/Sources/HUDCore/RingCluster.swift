import SwiftUI

/// One concentric ring cluster, outermost ring first. Each ring fills by the
/// consumed fraction of its window, colored by severity, drawn round-capped
/// over a hairline track. A fully spent limit therefore renders as a complete
/// solid ring. The two faces use the same view at different scales:
///   menubar mini: [session, weekly] at 18pt
///   notch face:   [session, weekly, fable] at 26pt (Codex has no fable ring)
public struct RingCluster: View {
    public let rings: [Window?]
    public var diameter: CGFloat = 18
    public var strokeWidth: CGFloat = 2
    /// When set, the cluster renders monochrome in this single color (faint
    /// track, solid fill) instead of the severity palette — for the menu-bar
    /// glance, which AppKit re-tints for contrast. Fill level still shows how
    /// spent a limit is; the color-coded severity lives in the full card.
    public var tint: Color?

    public init(rings: [Window?], diameter: CGFloat = 18, strokeWidth: CGFloat = 2, tint: Color? = nil) {
        self.rings = rings
        self.diameter = diameter
        self.strokeWidth = strokeWidth
        self.tint = tint
    }

    /// The original two-ring form used by the menubar mini and offline states.
    public init(session: Window?, weekly: Window?, diameter: CGFloat = 18, strokeWidth: CGFloat = 2, tint: Color? = nil) {
        self.init(rings: [session, weekly], diameter: diameter, strokeWidth: strokeWidth, tint: tint)
    }

    public var body: some View {
        ZStack {
            ForEach(Array(rings.enumerated()), id: \.offset) { index, window in
                ring(for: window, inset: inset(at: index))
            }
        }
        .frame(width: diameter, height: diameter)
    }

    /// Ring spacing from the approved artboards: at stroke 2 the radii step by
    /// 3pt per ring (26pt cluster: r 11/8/5; 18pt cluster: r 7/4), i.e. one
    /// stroke off the frame edge, then a stroke-and-a-point per level inward.
    private func inset(at index: Int) -> CGFloat {
        strokeWidth + CGFloat(index) * (strokeWidth + 1)
    }

    @ViewBuilder
    private func ring(for window: Window?, inset: CGFloat) -> some View {
        let fraction = Fmt.consumed(pctLeft: window?.pctLeft)
        let trackColor = tint.map { $0.opacity(0.32) } ?? Theme.hairline
        let fillColor = tint ?? Theme.severity(pctLeft: window?.pctLeft)
        ZStack {
            Circle()
                .stroke(trackColor, lineWidth: strokeWidth)
            Circle()
                .trim(from: 0, to: max(0, min(1, fraction)))
                .stroke(
                    fillColor,
                    style: StrokeStyle(lineWidth: strokeWidth, lineCap: .round)
                )
                .rotationEffect(.degrees(-90))
        }
        .padding(inset)
    }
}

/// The menu-bar status-item content: one ring cluster per subscription (Team,
/// Personal, Codex), each with its provider mark beside it — the Claude spark or
/// the Codex knot — so you can tell the plans apart at a glance. Each cluster is
/// the session ring outside, the tightest weekly inside, severity-colored, so a
/// plan running dry reads without opening the card. Clicking the item opens the
/// full card. Offline collapses to three dim rings.
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
                    HStack(spacing: 4) {
                        BrandMark(provider: sub.provider, size: 12, tint: tint)
                        RingCluster(rings: sub.miniRings, diameter: 18, strokeWidth: 2, tint: tint)
                    }
                }
            } else {
                ForEach(0..<3, id: \.self) { _ in
                    RingCluster(session: nil, weekly: nil, diameter: 18, tint: tint).opacity(0.35)
                }
            }
        }
        .padding(.horizontal, 5)
        .frame(height: 22)
    }
}
