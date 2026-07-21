import XCTest
@testable import HUDCore

/// Phase 4 coverage: the display-mode policy, the ring derivations feeding the
/// notch and menubar clusters, and a headless render of the notch pill.
final class NotchTests: XCTestCase {

    // MARK: - ModePolicy

    private func builtIn(notch: Bool) -> DisplayInfo {
        DisplayInfo(isBuiltIn: true, notchHeight: notch ? 37 : 0, notchWidth: notch ? 180 : 0)
    }
    private var external: DisplayInfo {
        DisplayInfo(isBuiltIn: false, notchHeight: 0, notchWidth: 0)
    }

    func testSoleNotchedBuiltInGetsNotchMode() {
        XCTAssertEqual(ModePolicy.mode(for: [builtIn(notch: true)]), .notch)
    }

    func testExternalAttachedFallsBackToMenubar() {
        XCTAssertEqual(ModePolicy.mode(for: [builtIn(notch: true), external]), .menubar)
    }

    func testClamshellExternalOnlyIsMenubar() {
        XCTAssertEqual(ModePolicy.mode(for: [external]), .menubar)
    }

    func testNotchlessBuiltInNeverGetsAFakeNotch() {
        XCTAssertEqual(ModePolicy.mode(for: [builtIn(notch: false)]), .menubar)
    }

    func testNoDisplaysIsMenubar() {
        XCTAssertEqual(ModePolicy.mode(for: []), .menubar)
    }

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
        // Fable at 12% left is tighter than 7d at 61%, so the 18pt mini shows it.
        let rings = sub(windows: [session, weekly, fable]).miniRings
        XCTAssertEqual(rings.count, 2)
        XCTAssertEqual(rings[1]?.kind, "weekly_fable")
    }

    // MARK: - Notch face geometry

    @MainActor
    func testNotchFaceWidthWrapsCamera() {
        let face = NotchFaceView(snapshot: nil, cameraWidth: 200)
        XCTAssertEqual(face.totalWidth, 200 + 2 * NotchFaceView.flankWidth)
    }

    // MARK: - Render smoke test

    @MainActor
    func testRendersNotchToNonEmptyPNG() throws {
        let out = FileManager.default.temporaryDirectory
            .appendingPathComponent("adash-hud-notch-test-\(UUID().uuidString).png")
        defer { try? FileManager.default.removeItem(at: out) }

        try PreviewRenderer.renderNotchPNG(to: out, scale: 2)

        let data = try Data(contentsOf: out)
        XCTAssertGreaterThan(data.count, 2000, "rendered PNG suspiciously small")
        XCTAssertEqual(Array(data.prefix(4)), [0x89, 0x50, 0x4E, 0x47])
    }
}
