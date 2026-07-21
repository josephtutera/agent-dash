import Foundation

// A hand-built snapshot used by the preview render and SwiftUI previews. It is
// deliberately shaped to exercise every visual state in the spec: a pressured
// sub with a pace line, a healthy sub, a spent Fable limit, waiting agents, and
// a full value block. The `now` is pinned so countdowns render deterministically.
extension HUDSnapshot {
    public static let previewNow: Date = {
        var c = DateComponents()
        c.year = 2026; c.month = 7; c.day = 21
        c.hour = 9; c.minute = 41; c.second = 0
        c.timeZone = TimeZone(identifier: "America/New_York")
        return Calendar(identifier: .gregorian).date(from: c)!
    }()

    private static func at(_ minutesFromNow: Int) -> Date {
        previewNow.addingTimeInterval(TimeInterval(minutesFromNow * 60))
    }

    public static let sample: HUDSnapshot = {
        let team = Subscription(
            id: "claude-team",
            provider: "claude",
            label: "Claude Team",
            windows: [
                Window(kind: "session_5h", pctLeft: 18, resetsAt: at(126),
                       pace: Pace(projectedDryAt: at(41), marginSeconds: 85 * 60)),
                Window(kind: "weekly_7d", pctLeft: 61, resetsAt: at(4 * 24 * 60 + 3 * 60), pace: nil),
            ],
            tightest: Window(kind: "session_5h", pctLeft: 18, resetsAt: at(126), pace: nil),
            stale: nil,
            activeAgents: 2
        )
        let personal = Subscription(
            id: "claude-personal",
            provider: "claude",
            label: "Claude Personal",
            windows: [
                Window(kind: "session_5h", pctLeft: 74, resetsAt: at(72), pace: nil),
                Window(kind: "weekly_7d", pctLeft: 88, resetsAt: at(5 * 24 * 60), pace: nil),
                Window(kind: "weekly_fable", pctLeft: 0, resetsAt: at(2 * 24 * 60 + 6 * 60), pace: nil),
            ],
            tightest: Window(kind: "weekly_fable", pctLeft: 0, resetsAt: at(2 * 24 * 60 + 6 * 60), pace: nil),
            stale: nil,
            activeAgents: 0
        )
        let codex = Subscription(
            id: "codex",
            provider: "codex",
            label: "Codex Max",
            windows: [
                Window(kind: "session_5h", pctLeft: 52, resetsAt: at(38), pace: nil),
                Window(kind: "weekly", pctLeft: 43, resetsAt: at(3 * 24 * 60 + 8 * 60), pace: nil),
            ],
            tightest: Window(kind: "session_5h", pctLeft: 52, resetsAt: at(38), pace: nil),
            stale: nil,
            activeAgents: 1
        )

        let agents = [
            Agent(pid: 4821, tool: "claude", project: "prototype-ehr",
                  cwd: "/Users/j/prototype-ehr", state: "waiting",
                  action: "waiting on approval to run tests", sinceSeconds: 72,
                  subscriptionID: "claude-team"),
            Agent(pid: 4822, tool: "claude", project: "web-app",
                  cwd: "/Users/j/web-app", state: "working",
                  action: "editing MeterRow.swift", sinceSeconds: 8 * 60 + 12,
                  subscriptionID: "claude-team"),
            Agent(pid: 5140, tool: "codex", project: "agent-dash",
                  cwd: "/Users/j/agent-dash", state: "working",
                  action: "running swift build", sinceSeconds: 44,
                  subscriptionID: "codex"),
            Agent(pid: 5602, tool: "opencode", project: "docs",
                  cwd: "/Users/j/docs", state: "idle",
                  action: nil, sinceSeconds: 61 * 60,
                  subscriptionID: nil),
        ]

        let value = ValueBlock(
            todayUSD: 182,
            monthUSD: 3140,
            subsCostUSD: 250,
            multiple: 12.6,
            bySub: [
                "claude-team": SubValue(todayUSD: 120, monthUSD: 2100),
                "claude-personal": SubValue(todayUSD: 22, monthUSD: 480),
                "codex": SubValue(todayUSD: 40, monthUSD: 560),
            ]
        )

        return HUDSnapshot(
            version: 1,
            generatedAt: previewNow,
            subscriptions: [team, personal, codex],
            agents: agents,
            value: value,
            soonestReset: SoonestReset(subscriptionID: "codex", kind: "session_5h", resetsAt: at(38))
        )
    }()
}
