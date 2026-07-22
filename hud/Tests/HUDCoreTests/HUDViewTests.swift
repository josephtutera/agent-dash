import XCTest
#if canImport(AppKit)
import AppKit
#endif
@testable import HUDCore

/// The ring derivations feeding the cluster views, the per-agent color
/// assignment, the tightest-quota derivation for the menu-bar glance, and a
/// headless render of that glance plus its card.
final class HUDViewTests: XCTestCase {

    // MARK: - Ring derivations

    private func sub(windows: [Window]) -> Subscription {
        Subscription(id: "claude-team", provider: "claude", label: "Claude Team",
                     windows: windows, tightest: nil, stale: nil, activeAgents: 0)
    }
    private let session = Window(kind: "session_5h", pctLeft: 40, resetsAt: nil, pace: nil)
    private let weekly = Window(kind: "weekly_7d", pctLeft: 61, resetsAt: nil, pace: nil)
    private let fable = Window(kind: "weekly_fable", pctLeft: 12, resetsAt: nil, pace: nil)
    private let codexWeekly = Window(kind: "weekly", pctLeft: 43, resetsAt: nil, pace: nil)

    func testNotchRingsAreSessionWeeklyFableForClaude() {
        let rings = sub(windows: [session, weekly, fable]).notchRings
        XCTAssertEqual(rings.count, 3)
        XCTAssertEqual(rings[0]?.kind, "session_5h")
        XCTAssertEqual(rings[1]?.kind, "weekly_7d")
        XCTAssertEqual(rings[2]?.kind, "weekly_fable")
    }

    func testNotchRingsDropFableRingWhenAbsent() {
        let rings = sub(windows: [session, codexWeekly]).notchRings
        XCTAssertEqual(rings.count, 2)
        XCTAssertEqual(rings[1]?.kind, "weekly")
    }

    func testWeekly7dNeverReturnsFable() {
        XCTAssertEqual(sub(windows: [session, fable]).weekly7dWindow?.kind, nil)
        XCTAssertEqual(sub(windows: [session, fable, weekly]).weekly7dWindow?.kind, "weekly_7d")
    }

    func testMiniInnerRingIsTightestWeekly() {
        let rings = sub(windows: [session, weekly, fable]).miniRings
        XCTAssertEqual(rings.count, 2)
        XCTAssertEqual(rings[1]?.kind, "weekly_fable")
    }

    // MARK: - Agent identity colors

    private func agent(pid: Int, state: String = "working") -> Agent {
        Agent(pid: pid, tool: "claude", project: "p\(pid)", cwd: "/tmp/p\(pid)",
              state: state, action: nil, sinceSeconds: nil, subscriptionID: nil)
    }

    func testAgentColorsAreDistinctPerAgent() {
        let colors = AgentColors.assign([agent(pid: 30), agent(pid: 10), agent(pid: 20)])
        XCTAssertEqual(colors.count, 3)
        let c10 = colors[10], c20 = colors[20], c30 = colors[30]
        XCTAssertNotNil(c10); XCTAssertNotNil(c20); XCTAssertNotNil(c30)
        XCTAssertNotEqual(c10, c20)
        XCTAssertNotEqual(c20, c30)
        XCTAssertNotEqual(c10, c30)
    }

    func testAgentColorsAssignByPidOrderNotInputOrder() {
        // Same set, different input order -> the same pid keeps the same color.
        let a = AgentColors.assign([agent(pid: 30), agent(pid: 10), agent(pid: 20)])
        let b = AgentColors.assign([agent(pid: 10), agent(pid: 20), agent(pid: 30)])
        XCTAssertEqual(a[10], b[10])
        XCTAssertEqual(a[20], b[20])
        XCTAssertEqual(a[30], b[30])
        XCTAssertEqual(a[10], Theme.agentColor(0))
        XCTAssertEqual(a[30], Theme.agentColor(2))
    }

    func testAgentStateHelpers() {
        XCTAssertTrue(agent(pid: 1, state: "waiting").isWaiting)
        XCTAssertTrue(agent(pid: 1, state: "working").isWorking)
        XCTAssertTrue(agent(pid: 1, state: "idle").isIdle)
        XCTAssertTrue(agent(pid: 1, state: "zombie").isIdle) // unknown -> idle
        XCTAssertFalse(agent(pid: 1, state: "working").isIdle)
    }

    // MARK: - Glance worst-quota ring

    private func subWithTightest(_ pct: Int?) -> Subscription {
        Subscription(id: "s\(pct ?? -1)", provider: "claude", label: "S",
                     windows: [], tightest: Window(kind: "session_5h", pctLeft: pct, resetsAt: nil, pace: nil),
                     stale: nil, activeAgents: 0)
    }

    func testWorstWindowIsTightestAcrossSubscriptions() {
        let snap = HUDSnapshot(version: 1, generatedAt: nil,
                               subscriptions: [subWithTightest(60), subWithTightest(8), subWithTightest(90)],
                               agents: [], value: nil, soonestReset: nil)
        XCTAssertEqual(snap.worstWindow?.pctLeft, 8)
    }

    func testWorstWindowNilWhenNoSubsReportTightest() {
        let snap = HUDSnapshot(version: 1, generatedAt: nil, subscriptions: [],
                               agents: [], value: nil, soonestReset: nil)
        XCTAssertNil(snap.worstWindow)
    }

    // MARK: - Render smoke test

    @MainActor
    func testRendersMenubarToNonEmptyPNG() throws {
        let out = FileManager.default.temporaryDirectory
            .appendingPathComponent("adash-hud-menubar-test-\(UUID().uuidString).png")
        defer { try? FileManager.default.removeItem(at: out) }

        try PreviewRenderer.renderMenubarPNG(to: out, scale: 2)

        let data = try Data(contentsOf: out)
        XCTAssertGreaterThan(data.count, 2000, "rendered PNG suspiciously small")
        XCTAssertEqual(Array(data.prefix(4)), [0x89, 0x50, 0x4E, 0x47])
    }

    // MARK: - Embedded brand-mark assets

    #if canImport(AppKit)
    func testBrandMarkAssetsDecodeToTemplateImages() {
        for (name, base64, image) in [
            ("Claude", ClaudeMarkAsset.pngBase64, ClaudeMark.templateImage),
            ("OpenAI", OpenAIMarkAsset.pngBase64, OpenAIMark.templateImage),
        ] {
            let data = Data(base64Encoded: base64, options: .ignoreUnknownCharacters)
            XCTAssertNotNil(data, "\(name) mark base64 must decode")
            XCTAssertGreaterThan(data?.count ?? 0, 500, "\(name) PNG suspiciously small")
            XCTAssertEqual(Array(data!.prefix(4)), [0x89, 0x50, 0x4E, 0x47], "\(name) not a PNG")
            XCTAssertTrue(image.isTemplate, "\(name) mark must be a template so it takes the tint")
            XCTAssertGreaterThan(image.size.width, 0)
            XCTAssertGreaterThan(image.size.height, 0)
        }
    }
    #endif
}
