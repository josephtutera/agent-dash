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

    public init(rings: [Window?], diameter: CGFloat = 18, strokeWidth: CGFloat = 2) {
        self.rings = rings
        self.diameter = diameter
        self.strokeWidth = strokeWidth
    }

    /// The original two-ring form used by the menubar mini and offline states.
    public init(session: Window?, weekly: Window?, diameter: CGFloat = 18, strokeWidth: CGFloat = 2) {
        self.init(rings: [session, weekly], diameter: diameter, strokeWidth: strokeWidth)
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
        let color = Theme.severity(pctLeft: window?.pctLeft)
        ZStack {
            Circle()
                .stroke(Theme.hairline, lineWidth: strokeWidth)
            Circle()
                .trim(from: 0, to: max(0, min(1, fraction)))
                .stroke(
                    color,
                    style: StrokeStyle(lineWidth: strokeWidth, lineCap: .round)
                )
                .rotationEffect(.degrees(-90))
        }
        .padding(inset)
    }
}

/// The full menubar item: the three mini clusters (Team, Personal, Codex) plus
/// the soonest-reset countdown in monospaced amber. Offline collapses to three
/// gray rings and no countdown.
public struct MenuBarContentView: View {
    public let snapshot: HUDSnapshot?
    public var now: Date

    public init(snapshot: HUDSnapshot?, now: Date = Date()) {
        self.snapshot = snapshot
        self.now = now
    }

    // Fixed presentation order regardless of daemon ordering.
    private static let order = ["claude-team", "claude-personal", "codex"]

    private var orderedSubs: [Subscription] {
        guard let snap = snapshot else { return [] }
        return Self.order.compactMap { id in snap.subscriptions.first { $0.id == id } }
    }

    public var body: some View {
        HStack(spacing: 5) {
            if let snap = snapshot, !orderedSubs.isEmpty {
                ForEach(orderedSubs) { sub in
                    RingCluster(rings: sub.miniRings)
                        .opacity(sub.clusterOpacity)
                }
                if let soonest = snap.soonestReset {
                    Text(Fmt.countdown(to: soonest.resetsAt, now: now))
                        .font(Theme.mono(11, weight: .medium))
                        .foregroundStyle(Theme.amber)
                        .monospacedDigit()
                }
            } else {
                // Offline: three gray rings, no countdown.
                ForEach(0..<3, id: \.self) { _ in
                    RingCluster(session: nil, weekly: nil)
                        .opacity(0.35)
                }
            }
        }
        .padding(.horizontal, 4)
        .frame(height: 22)
    }
}
