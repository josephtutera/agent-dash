import SwiftUI

/// One mini concentric ring cluster: outer ring = 5h session, inner ring =
/// weekly. Each ring fills by the consumed fraction of its limit, colored by
/// severity, drawn round-capped over a hairline track. A fully spent limit
/// therefore renders as a complete solid ring.
public struct RingCluster: View {
    public let session: Window?
    public let weekly: Window?
    public var diameter: CGFloat = 18
    public var strokeWidth: CGFloat = 2

    public init(session: Window?, weekly: Window?, diameter: CGFloat = 18, strokeWidth: CGFloat = 2) {
        self.session = session
        self.weekly = weekly
        self.diameter = diameter
        self.strokeWidth = strokeWidth
    }

    public var body: some View {
        ZStack {
            ring(for: session, inset: 0)
            ring(for: weekly, inset: strokeWidth + 1.5)
        }
        .frame(width: diameter, height: diameter)
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
                    RingCluster(session: sub.sessionWindow, weekly: sub.weeklyWindow)
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
