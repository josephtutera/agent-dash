import SwiftUI

// View-facing derivations over the raw contract structs. Kept out of the views
// so they can be unit tested and so the rendering code stays declarative.

extension Window {
    public var isSession: Bool { kind == "session_5h" }
    public var isWeekly: Bool { kind.hasPrefix("weekly") }
    public var isFable: Bool { kind == "weekly_fable" }
    public var isLimitReached: Bool { (pctLeft ?? 100) <= 0 }
}

extension Subscription {
    /// Outer ring source: the 5h session window.
    public var sessionWindow: Window? {
        windows.first { $0.isSession }
    }

    /// Inner ring source: the first weekly window (7d, or Fable for that sub).
    public var weeklyWindow: Window? {
        windows.first { $0.isWeekly }
    }

    /// The plain weekly window (Claude `weekly_7d`, Codex `weekly`), never Fable.
    public var weekly7dWindow: Window? {
        windows.first { $0.isWeekly && !$0.isFable }
    }

    /// The model-scoped Fable weekly window, if this subscription has one.
    public var fableWindow: Window? {
        windows.first { $0.isFable }
    }

    /// The notch-face cluster rings, outer to inner: 5h session, weekly, then
    /// Fable when the subscription reports one (Codex clusters get two rings).
    public var notchRings: [Window?] {
        var rings: [Window?] = [sessionWindow, weekly7dWindow]
        if let fable = fableWindow { rings.append(fable) }
        return rings
    }

    /// The single window the menu-bar glance headlines: the 5h session, i.e.
    /// the immediate "can I work right now" budget. Falls back to the tightest
    /// window, then the first, so every subscription resolves to one number.
    /// A dry weekly/Fable limit is not surfaced here by design; it reads in the
    /// popover a click away, where every window is shown.
    public var glanceWindow: Window? {
        sessionWindow ?? tightest ?? windows.first
    }

    public var isIdle: Bool { activeAgents <= 0 }

    public var hasLimitReached: Bool {
        windows.contains { $0.isLimitReached }
    }

    /// Cluster opacity: full when working, dim when idle, but a spent limit
    /// stays bright enough for the red to still read.
    public var clusterOpacity: Double {
        if hasLimitReached { return 0.55 }
        return isIdle ? 0.4 : 1.0
    }

    /// The right-aligned status string for the section header and its color.
    public func status(now: Date = Date()) -> (text: String, color: Color) {
        if hasLimitReached {
            return ("limit reached", Theme.red)
        }
        if let tight = tightest, let pct = tight.pctLeft, pct < 25 {
            if let reset = tight.resetsAt {
                let label = Fmt.windowLabel(kind: tight.kind)
                return ("\(label) resets \(Fmt.clock(reset))", Theme.amber)
            }
            return ("pressured", Theme.amber)
        }
        return ("healthy", Theme.green)
    }
}

extension Agent {
    public var isWaiting: Bool { state == "waiting" }
    public var isWorking: Bool { state == "working" }
    public var isIdle: Bool { !isWaiting && !isWorking }
}

/// Assigns one distinct color per running agent. Agents are colored in pid
/// order so the mapping is deterministic across polls and every currently
/// visible agent gets a different hue (until the palette wraps). Both the notch
/// face and the dropdown list resolve through here, so a color in the bar maps
/// to the same-colored row in the card.
public enum AgentColors {
    public static func assign(_ agents: [Agent]) -> [Int: Color] {
        var map: [Int: Color] = [:]
        for (i, agent) in agents.sorted(by: { $0.pid < $1.pid }).enumerated() {
            map[agent.pid] = Theme.agentColor(i)
        }
        return map
    }
}

extension HUDSnapshot {
    public var waitingAgentCount: Int {
        agents.filter { $0.isWaiting }.count
    }

    /// Agents the card actually shows: currently working or waiting. Idle and
    /// stale sessions are dropped so the list can't go out of date — it only
    /// ever holds live work, ordered as the daemon sent it.
    public var runningAgents: [Agent] {
        agents.filter { !$0.isIdle }
    }

    /// The single tightest window across every subscription, spent limits
    /// included, for the one severity ring the compact collapsed face carries so
    /// a plan running dry still warns from the menu bar without the full card.
    public var worstWindow: Window? {
        subscriptions
            .compactMap { $0.tightest }
            .min { ($0.pctLeft ?? 101) < ($1.pctLeft ?? 101) }
    }

    /// The single tightest *live* window across all subscriptions, with its
    /// owning sub, used for the one pace line under the card's meters. Spent
    /// limits (0% left) are excluded: a "dry at" projection is meaningless for
    /// a window that is already dead, so pace belongs to the tightest window
    /// that still has headroom.
    public var overallTightest: (sub: Subscription, window: Window)? {
        var best: (Subscription, Window)?
        for sub in subscriptions {
            guard let t = sub.tightest, let pct = t.pctLeft, pct > 0 else { continue }
            if let (_, bw) = best, let bp = bw.pctLeft, bp <= pct { continue }
            best = (sub, t)
        }
        // tightest carries no pace; find the matching window that does.
        guard let (sub, t) = best else { return nil }
        let full = sub.windows.first { $0.kind == t.kind } ?? t
        return (sub, full)
    }
}
