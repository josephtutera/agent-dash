import SwiftUI

/// One agent indicator for the notch face: a filled dot in the agent's own
/// color (its identity) wrapped by a ring that carries state — a sweeping
/// spinner while it works, an amber pip when it's waiting on you, dimmed when
/// idle. Color is identity, motion/pip/opacity is state, so two Claude sessions
/// are still tellable apart.
struct AgentDot: View {
    let agent: Agent
    let color: Color
    var diameter: CGFloat = 18

    @State private var spin = false

    private var stroke: CGFloat { max(1.5, diameter * 0.1) }

    var body: some View {
        ZStack {
            if agent.isWorking {
                Circle()
                    .trim(from: 0, to: 0.72)
                    .stroke(color, style: StrokeStyle(lineWidth: stroke, lineCap: .round))
                    .rotationEffect(.degrees(spin ? 360 : 0))
                    .animation(.linear(duration: 1.1).repeatForever(autoreverses: false), value: spin)
                    .onAppear { spin = true }
            } else {
                Circle()
                    .stroke(color.opacity(0.5), lineWidth: stroke)
            }

            Circle()
                .fill(color)
                .frame(width: diameter * 0.44, height: diameter * 0.44)

            if agent.isWaiting {
                Circle()
                    .fill(Theme.amber)
                    .frame(width: diameter * 0.34, height: diameter * 0.34)
                    .overlay(Circle().stroke(Theme.notch, lineWidth: 1))
                    .offset(x: diameter * 0.42, y: -diameter * 0.42)
            }
        }
        .frame(width: diameter, height: diameter)
        .opacity(agent.isIdle ? 0.5 : 1.0)
    }
}

/// The dashed "+" that opens the launcher. Always present, so a new session is
/// one click away whether or not anything is running.
struct LaunchChip: View {
    var diameter: CGFloat = 18

    var body: some View {
        ZStack {
            Circle()
                .strokeBorder(style: StrokeStyle(lineWidth: 1.4, dash: [2.4, 2.2]))
                .foregroundStyle(Theme.muted)
            Image(systemName: "plus")
                .font(.system(size: diameter * 0.55, weight: .semibold))
                .foregroundStyle(Theme.muted)
        }
        .frame(width: diameter, height: diameter)
    }
}

/// The running-agent strip for the notch's right flank: one colored dot per
/// live agent (pid order, so colors are stable and distinct) plus the launcher
/// chip. Replaces the old countdown — motion on the right now means work is
/// happening.
public struct AgentClusterView: View {
    public let agents: [Agent]
    public var diameter: CGFloat
    public var onSelect: ((Agent) -> Void)?
    public var onLaunch: (() -> Void)?

    public init(
        agents: [Agent],
        diameter: CGFloat = 18,
        onSelect: ((Agent) -> Void)? = nil,
        onLaunch: (() -> Void)? = nil
    ) {
        self.agents = agents
        self.diameter = diameter
        self.onSelect = onSelect
        self.onLaunch = onLaunch
    }

    private var ordered: [Agent] { agents.sorted { $0.pid < $1.pid } }
    private var colors: [Int: Color] { AgentColors.assign(agents) }

    public var body: some View {
        HStack(spacing: diameter * 0.5) {
            ForEach(ordered) { agent in
                AgentDot(agent: agent, color: colors[agent.pid] ?? Theme.muted, diameter: diameter)
                    .contentShape(Rectangle())
                    .onTapGesture { onSelect?(agent) }
            }
            LaunchChip(diameter: diameter)
                .contentShape(Rectangle())
                .onTapGesture { onLaunch?() }
        }
    }
}
