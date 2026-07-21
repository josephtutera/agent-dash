import SwiftUI
#if canImport(AppKit)
import AppKit
#endif

// The click-through card. A panel-glass surface with, top to bottom: one
// section per subscription, an AGENTS section, a VALUE AT API RATES section,
// and a footer. Width ~460, radius 14, hairline border.

public struct PopoverCard: View {
    public let snapshot: HUDSnapshot?
    public var now: Date

    public init(snapshot: HUDSnapshot?, now: Date = Date()) {
        self.snapshot = snapshot
        self.now = now
    }

    private static let order = ["claude-team", "claude-personal", "codex"]

    private var orderedSubs: [Subscription] {
        guard let snap = snapshot else { return [] }
        return Self.order.compactMap { id in snap.subscriptions.first { $0.id == id } }
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if let snap = snapshot {
                ForEach(orderedSubs) { sub in
                    SubscriptionSectionView(sub: sub, tightest: snap.overallTightest, now: now)
                }
                AgentsSectionView(agents: snap.agents, now: now)
                if let value = snap.value {
                    ValueSectionView(value: value, now: now)
                }
                FooterView(generatedAt: snap.generatedAt, now: now)
            } else {
                OfflineView()
            }
        }
        .padding(18)
        .frame(width: 460, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Theme.panel)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(Theme.hairline, lineWidth: 1)
        )
    }
}

// MARK: - Section header rule

/// A section rule: a caps letterspaced label, a hairline that fills the row,
/// and an optional right-aligned accessory.
struct SectionRule<Accessory: View>: View {
    let title: String
    @ViewBuilder var accessory: () -> Accessory

    var body: some View {
        HStack(spacing: 10) {
            Text(title)
                .font(Theme.label(10, weight: .semibold))
                .tracking(1.4)
                .foregroundStyle(Theme.muted)
            Rectangle()
                .fill(Theme.hairline)
                .frame(height: 1)
            accessory()
        }
    }
}

extension SectionRule where Accessory == EmptyView {
    init(title: String) {
        self.init(title: title, accessory: { EmptyView() })
    }
}

// MARK: - Subscription section

struct SubscriptionSectionView: View {
    let sub: Subscription
    let tightest: (sub: Subscription, window: Window)?
    let now: Date

    private var status: (text: String, color: Color) { sub.status(now: now) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // (a) Header row: mark + name caps + rule + status.
            HStack(spacing: 8) {
                BrandMark(provider: sub.provider, size: 13)
                Text(sub.label.uppercased())
                    .font(Theme.label(10, weight: .semibold))
                    .tracking(1.2)
                    .foregroundStyle(Theme.text)
                Rectangle()
                    .fill(Theme.hairline)
                    .frame(height: 1)
                Text(statusText)
                    .font(Theme.mono(10))
                    .foregroundStyle(status.color)
                    .monospacedDigit()
                    .fixedSize()
            }

            // (b) One meter row per window.
            ForEach(Array(sub.windows.enumerated()), id: \.offset) { _, window in
                MeterRow(window: window, dimmed: sub.stale != nil, now: now)
            }

            // (c) Pace line, only under the single overall-tightest window.
            if let tight = tightest,
               tight.sub.id == sub.id,
               let pace = tight.window.pace {
                Text(paceLine(pace: pace, window: tight.window))
                    .font(Theme.label(10))
                    .foregroundStyle(Theme.muted)
                    .padding(.leading, 34)
            }
        }
    }

    private var statusText: String {
        if let reason = sub.stale {
            return "stale · \(reason)"
        }
        return status.text
    }

    private func paceLine(pace: Pace, window: Window) -> String {
        let dry = Fmt.clock(pace.projectedDryAt)
        let margin = Fmt.sinceLabel(seconds: pace.marginSeconds)
        return "at this pace, dry \(dry), \(margin) before reset"
    }
}

// MARK: - Meter row

struct MeterRow: View {
    let window: Window
    let dimmed: Bool
    let now: Date

    var body: some View {
        HStack(spacing: 10) {
            Text(Fmt.windowLabel(kind: window.kind))
                .font(Theme.mono(11))
                .foregroundStyle(Theme.muted)
                .frame(width: 26, alignment: .leading)

            ProgressBar(pctLeft: window.pctLeft)
                .frame(height: 4)

            Text(Fmt.meterValue(pctLeft: window.pctLeft, resetsAt: window.resetsAt, now: now))
                .font(Theme.mono(11))
                .foregroundStyle(Theme.text)
                .monospacedDigit()
                .frame(width: 96, alignment: .trailing)
        }
        .opacity(dimmed ? 0.5 : 1.0)
    }
}

/// A 4pt rounded progress bar. Fill = consumed fraction, severity-colored, with
/// a soft glow on amber/red fills so pressure reads at a glance.
struct ProgressBar: View {
    let pctLeft: Int?

    var body: some View {
        GeometryReader { geo in
            let fraction = Fmt.consumed(pctLeft: pctLeft)
            let color = Theme.severity(pctLeft: pctLeft)
            let glow = (pctLeft ?? 100) < 25
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.hairline)
                Capsule()
                    .fill(color)
                    .frame(width: max(0, min(1, fraction)) * geo.size.width)
                    .shadow(color: glow ? color.opacity(0.7) : .clear, radius: glow ? 4 : 0)
            }
        }
    }
}

// MARK: - Agents section

struct AgentsSectionView: View {
    let agents: [Agent]
    let now: Date

    private var waiting: Int { agents.filter { $0.isWaiting }.count }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionRule(title: "AGENTS") {
                if waiting > 0 {
                    Text("\(waiting) needs you")
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.amber)
                        .fixedSize()
                }
            }
            ForEach(agents) { agent in
                AgentRow(agent: agent, color: colors[agent.pid] ?? Theme.toolColor(agent.tool), now: now)
            }
        }
    }

    // Same assignment the notch face uses, so a color in the bar is the same
    // color on the row here.
    private var colors: [Int: Color] { AgentColors.assign(agents) }
}

struct AgentRow: View {
    let agent: Agent
    let color: Color
    let now: Date

    var body: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(color)
                .frame(width: 7, height: 7)

            Text(agent.project)
                .font(Theme.label(12, weight: .semibold))
                .foregroundStyle(Theme.text)

            if let action = agent.action {
                Text(action)
                    .font(Theme.label(12))
                    .foregroundStyle(agent.isWaiting ? Theme.amber : Theme.muted)
                    .lineLimit(1)
            }

            Spacer(minLength: 8)

            Text(Fmt.sinceLabel(seconds: agent.sinceSeconds))
                .font(Theme.mono(11))
                .foregroundStyle(Theme.muted)
                .monospacedDigit()
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(
            // Waiting rows get a faint amber wash so they pull the eye.
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(agent.isWaiting ? Theme.amber.opacity(0.10) : Color.clear)
        )
        // TODO(phase4): clicking a row should focus/resume this agent's session.
        .contentShape(Rectangle())
        .onTapGesture { /* no-op for now */ }
    }
}

// MARK: - Value section

struct ValueSectionView: View {
    let value: ValueBlock
    let now: Date

    private var monthName: String {
        let f = DateFormatter()
        f.dateFormat = "MMMM"
        return f.string(from: now).uppercased()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionRule(title: "VALUE AT API RATES") {
                if let m = value.multiple {
                    Text("\(Fmt.multiple(m)) your subs")
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.green)
                        .fixedSize()
                }
            }
            HStack(spacing: 8) {
                StatTile(caption: "TODAY", value: Fmt.usd(value.todayUSD))
                StatTile(caption: monthName, value: Fmt.usd(value.monthUSD))
                StatTile(
                    caption: "SUBS COST",
                    value: value.subsCostUSD.map(Fmt.usd) ?? "--"
                )
            }
        }
    }
}

struct StatTile: View {
    let caption: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(caption)
                .font(Theme.label(9, weight: .semibold))
                .tracking(1.0)
                .foregroundStyle(Theme.muted)
            Text(value)
                .font(Theme.mono(14, weight: .medium))
                .foregroundStyle(Theme.text)
                .monospacedDigit()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Theme.panel2)
        )
    }
}

// MARK: - Footer

struct FooterView: View {
    let generatedAt: Date?
    let now: Date

    var body: some View {
        HStack {
            Text("updated \(generatedAt.map(Fmt.clock) ?? "--")")
                .font(Theme.label(10))
                .foregroundStyle(Theme.muted)
            Spacer()
            // Quit: an accessory app has no dock icon or app menu, so the card
            // (and the pill's right-click menu) are the only ways out.
            HStack(spacing: 4) {
                Image(systemName: "power")
                    .font(.system(size: 9, weight: .semibold))
                Text("quit")
                    .font(Theme.label(10))
            }
            .foregroundStyle(Theme.muted)
            .contentShape(Rectangle())
            .onTapGesture { AppActions.quit() }
            .padding(.trailing, 12)
            HStack(spacing: 6) {
                Text("open agent dash")
                    .font(Theme.label(10))
                    .foregroundStyle(Theme.muted)
                Text("⏎")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.text)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .overlay(
                        RoundedRectangle(cornerRadius: 4, style: .continuous)
                            .strokeBorder(Theme.hairline, lineWidth: 1)
                    )
            }
            // TODO(phase4): replace `open -a Warp` placeholder with the real
            // agent-dash launch.
            .contentShape(Rectangle())
            .onTapGesture { AppActions.openAgentDash() }
        }
        .padding(.top, 2)
    }
}

// MARK: - Offline

struct OfflineView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 5) {
                ForEach(0..<3, id: \.self) { _ in
                    RingCluster(session: nil, weekly: nil).opacity(0.35)
                }
            }
            Text("daemon offline, run: adash serve")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.muted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 8)
    }
}

/// Side effects the card triggers. Kept behind a seam so the views stay pure
/// and the tests never shell out.
public enum AppActions {
    public static func openAgentDash() {
        #if canImport(AppKit)
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        proc.arguments = ["-a", "Warp"] // TODO(phase4): real agent-dash target
        try? proc.run()
        #endif
    }

    /// Jump to a running agent's terminal tab. Wired to the daemon in phase 3;
    /// for now it surfaces Agent Dash so the click is never a dead end.
    public static func jumpToAgent(_ agent: Agent) {
        openAgentDash()
    }

    /// Open the new-session launcher (pick a tool + directory). Wired in phase 3.
    public static func openLauncher() {
        openAgentDash()
    }

    /// Quit the HUD. There's no dock icon or app menu (it's an accessory app),
    /// so this is the only way out short of `kill`; the footer button and the
    /// pill's right-click menu both call it.
    public static func quit() {
        #if canImport(AppKit)
        NSApplication.shared.terminate(nil)
        #endif
    }
}
