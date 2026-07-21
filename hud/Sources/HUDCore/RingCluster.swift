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

/// The menu-bar status-item content: the glance. One colored dot per running
/// agent (spinning while it works, amber pip when it needs you, dim when idle)
/// followed by a single severity ring for the tightest quota, so a plan running
/// dry still warns. Clicking the item opens the full card. Offline collapses to
/// one dim ring. The dots here are non-interactive — the whole status item is
/// one click target — so they carry no tap gestures.
public struct MenuBarContentView: View {
    public let snapshot: HUDSnapshot?
    public var now: Date

    public init(snapshot: HUDSnapshot?, now: Date = Date()) {
        self.snapshot = snapshot
        self.now = now
    }

    private var agents: [Agent] {
        (snapshot?.agents ?? []).sorted { $0.pid < $1.pid }
    }

    public var body: some View {
        HStack(spacing: 7) {
            if let snap = snapshot {
                let colors = AgentColors.assign(snap.agents)
                ForEach(agents) { agent in
                    AgentDot(agent: agent, color: colors[agent.pid] ?? Theme.muted, diameter: 16)
                }
                if let worst = snap.worstWindow {
                    RingCluster(rings: [worst], diameter: 16, strokeWidth: 2)
                }
            } else {
                RingCluster(session: nil, weekly: nil, diameter: 16).opacity(0.35)
            }
        }
        .padding(.horizontal, 5)
        .frame(height: 22)
    }
}
