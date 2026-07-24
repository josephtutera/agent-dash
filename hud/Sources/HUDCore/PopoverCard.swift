import SwiftUI
#if canImport(AppKit)
import AppKit
#endif

// The click-through card, cockpit layout. Top to bottom: a slim header, a gauge
// cluster (one pod per subscription with a big session readout + fuel bar), a
// pace-warning strip, the active agents, a value strip, and a footer. Colors are
// the dynamic Theme tokens, so the whole card follows the system light/dark
// appearance. Width 460, radius 14, hairline border.

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
        VStack(alignment: .leading, spacing: 16) {
            if let snap = snapshot {
                CardHeaderView(now: now)
                GaugeClusterView(subs: orderedSubs, now: now)
                if let tight = snap.overallTightest, let pace = tight.window.pace {
                    PaceStripView(sub: tight.sub, pace: pace, now: now)
                }
                AgentsSectionView(agents: snap.runningAgents, now: now)
                if let value = snap.value {
                    ValueStripView(value: value, now: now)
                }
                FooterView(generatedAt: snap.generatedAt, now: now)
            } else {
                OfflineView()
            }
        }
        .padding(16)
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

// MARK: - Header

/// A slim card head: the wordmark, a hairline that fills the row, and the
/// current date and time, so the card reads as a dated snapshot.
struct CardHeaderView: View {
    let now: Date

    var body: some View {
        HStack(spacing: 10) {
            Text("AGENT DASH")
                .font(Theme.label(11, weight: .semibold))
                .tracking(2.0)
                .foregroundStyle(Theme.text)
            Rectangle().fill(Theme.hairline).frame(height: 1)
            Text(Self.dateText(now))
                .font(Theme.mono(10))
                .foregroundStyle(Theme.muted)
                .fixedSize()
        }
    }

    static func dateText(_ now: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "EEE MMM d"
        return "\(f.string(from: now)) · \(Fmt.clock(now))"
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

// MARK: - Gauge cluster

/// The instrument cluster: one gauge pod per subscription, equal width.
struct GaugeClusterView: View {
    let subs: [Subscription]
    let now: Date

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            ForEach(subs) { sub in
                PodView(sub: sub, now: now)
            }
        }
    }
}

/// One plan's gauge pod: mark + short name, the big session percent, a fuel bar
/// (filled by consumed, severity-colored), and a two-line caption (session
/// detail, then the weekly window with a red flag if an extra/Fable limit is
/// spent). When the session window is pressured (< 25% left) the whole pod
/// lights in its severity color, the way a dashboard warning lamp does.
struct PodView: View {
    let sub: Subscription
    let now: Date

    private var sessionWindow: Window? { sub.sessionWindow ?? sub.tightest ?? sub.windows.first }
    private var sessionPct: Int? { sessionWindow?.pctLeft }
    private var severity: Color { Theme.severity(pctLeft: sessionPct) }

    /// Lit when the session window is amber/red (pressured), i.e. < 25% left.
    private var isLit: Bool {
        guard let p = sessionPct else { return false }
        return p < 25
    }

    private var shortName: String {
        if sub.label.hasPrefix("Claude ") {
            return String(sub.label.dropFirst("Claude ".count))
        }
        if sub.provider == "codex" { return "Codex" }
        return sub.label
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 6) {
                BrandMark(provider: sub.provider, size: 12, tint: Theme.providerColor(sub.provider))
                Text(shortName.uppercased())
                    .font(Theme.label(9, weight: .semibold))
                    .tracking(1.0)
                    .foregroundStyle(Theme.muted)
            }

            HStack(alignment: .firstTextBaseline, spacing: 2) {
                Text(Fmt.glancePercent(pctLeft: sessionPct))
                    .font(Theme.mono(32, weight: .semibold))
                    .foregroundStyle(isLit ? severity : Theme.text)
                    .monospacedDigit()
                Text("%")
                    .font(Theme.mono(12))
                    .foregroundStyle(isLit ? severity.opacity(0.7) : Theme.muted)
            }

            PodFuelBar(pctLeft: sessionPct)

            VStack(alignment: .leading, spacing: 2) {
                Text(sessionCaption)
                    .font(Theme.mono(9.5))
                    .foregroundStyle(Theme.muted)
                weeklyCaption
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(isLit ? severity.opacity(0.10) : Theme.panel2)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(isLit ? severity.opacity(0.40) : Theme.hairline, lineWidth: 1)
        )
    }

    private var sessionCaption: String {
        guard let w = sessionWindow else { return "" }
        let label = Fmt.windowLabel(kind: w.kind)
        if let reset = w.resetsAt {
            return "\(label) · \(Fmt.countdown(to: reset, now: now)) left"
        }
        return label
    }

    @ViewBuilder
    private var weeklyCaption: some View {
        let fableMaxed = sub.fableWindow?.isLimitReached ?? false
        HStack(spacing: 0) {
            Text(weeklyText)
                .font(Theme.mono(9.5))
                .foregroundStyle(Theme.faint)
            if fableMaxed {
                Text(" · ")
                    .font(Theme.mono(9.5))
                    .foregroundStyle(Theme.faint)
                Text("F maxed")
                    .font(Theme.mono(9.5))
                    .foregroundStyle(Theme.red)
            }
        }
    }

    private var weeklyText: String {
        guard let w = sub.weekly7dWindow else { return "" }
        let label = Fmt.windowLabel(kind: w.kind)
        let pct = w.pctLeft.map { "\($0)%" } ?? "--"
        return "\(label) · \(pct)"
    }
}

/// A 4pt rounded fuel bar for a pod: fill = consumed fraction, severity-colored.
struct PodFuelBar: View {
    let pctLeft: Int?

    var body: some View {
        GeometryReader { geo in
            let fraction = Fmt.consumed(pctLeft: pctLeft)
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.hairline)
                Capsule()
                    .fill(Theme.severity(pctLeft: pctLeft))
                    .frame(width: max(0, min(1, fraction)) * geo.size.width)
            }
        }
        .frame(height: 4)
    }
}

// MARK: - Pace strip

/// A one-line warning for the single most-pressured live window across all
/// plans: when it will run dry at the current pace, and how much margin is left
/// before its reset saves it.
struct PaceStripView: View {
    let sub: Subscription
    let pace: Pace
    let now: Date

    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 11))
                .foregroundStyle(Theme.amber)
            (
                Text("\(sub.label) runs dry ").foregroundStyle(Theme.text)
                + Text(Fmt.clock(pace.projectedDryAt)).foregroundStyle(Theme.amber).bold()
                + Text(" at this pace — \(Fmt.sinceLabel(seconds: pace.marginSeconds)) before reset")
                    .foregroundStyle(Theme.text)
            )
            .font(Theme.label(11))
            .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 13)
        .padding(.vertical, 10)
        .background(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(Theme.amber.opacity(0.10))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(Theme.amber.opacity(0.22), lineWidth: 1)
        )
    }
}

// MARK: - Agents section

/// The running agents (already filtered to active by `HUDSnapshot.runningAgents`).
/// Colors are assigned over exactly this set, and the section hides itself when
/// nothing is running.
struct AgentsSectionView: View {
    let agents: [Agent]
    let now: Date

    private var waiting: Int { agents.filter { $0.isWaiting }.count }
    private var colors: [Int: Color] { AgentColors.assign(agents) }

    var body: some View {
        if !agents.isEmpty {
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
    }
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
                    .truncationMode(.tail)
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
        .contentShape(Rectangle())
        .onTapGesture { AppActions.jumpToAgent(agent) }
    }
}

// MARK: - Value strip

/// The API-value readout as a trip-computer strip: equal segments for today,
/// the month, and the value multiple, divided by hairlines.
struct ValueStripView: View {
    let value: ValueBlock
    let now: Date

    private var monthName: String {
        let f = DateFormatter()
        f.dateFormat = "MMMM"
        return f.string(from: now).uppercased()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 11) {
            SectionRule(title: "VALUE AT API RATES")
            HStack(spacing: 0) {
                segment(caption: "TODAY", value: Fmt.usd(value.todayUSD), color: Theme.text)
                divider
                segment(caption: monthName, value: Fmt.usd(value.monthUSD), color: Theme.text)
                divider
                segment(
                    caption: "MULTIPLE",
                    value: value.multiple.map(Fmt.multiple) ?? "--",
                    color: Theme.green
                )
            }
            .background(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(Theme.panel2)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(Theme.hairline, lineWidth: 1)
            )
        }
    }

    private func segment(caption: String, value: String, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(caption)
                .font(Theme.label(9, weight: .semibold))
                .tracking(1.0)
                .foregroundStyle(Theme.muted)
            Text(value)
                .font(Theme.mono(16, weight: .medium))
                .foregroundStyle(color)
                .monospacedDigit()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
    }

    private var divider: some View {
        Rectangle().fill(Theme.hairline).frame(width: 1)
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
