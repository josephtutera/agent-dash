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

    /// The menubar mini rings: 5h session outside, the tightest weekly inside,
    /// so a pressured Fable limit still reads at the 18pt scale where a third
    /// ring would be too small to draw.
    public var miniRings: [Window?] {
        let weeklies = windows.filter { $0.isWeekly }
        let tightestWeekly = weeklies.min { a, b in
            (a.pctLeft ?? 101) < (b.pctLeft ?? 101)
        }
        return [sessionWindow, tightestWeekly ?? weeklyWindow]
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
}

extension HUDSnapshot {
    public var waitingAgentCount: Int {
        agents.filter { $0.isWaiting }.count
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
