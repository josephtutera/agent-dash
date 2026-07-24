import XCTest
import SwiftUI
@testable import HUDCore

final class FormattingTests: XCTestCase {

    private let now = Date(timeIntervalSince1970: 1_700_000_000)

    private func plus(_ seconds: TimeInterval) -> Date {
        now.addingTimeInterval(seconds)
    }

    // MARK: Countdown ladder

    func testCountdownSubHourShowsMinutes() {
        XCTAssertEqual(Fmt.countdown(to: plus(37 * 60), now: now), "37m")
        XCTAssertEqual(Fmt.countdown(to: plus(59 * 60 + 59), now: now), "59m")
    }

    func testCountdownUnderOneMinuteFloorsToOne() {
        XCTAssertEqual(Fmt.countdown(to: plus(20), now: now), "1m")
    }

    func testCountdownHoursZeroPadsMinutes() {
        // 2h06m
        XCTAssertEqual(Fmt.countdown(to: plus(2 * 3600 + 6 * 60), now: now), "2h06")
        // exactly one hour
        XCTAssertEqual(Fmt.countdown(to: plus(3600), now: now), "1h00")
        // 5h30m
        XCTAssertEqual(Fmt.countdown(to: plus(5 * 3600 + 30 * 60), now: now), "5h30")
    }

    func testCountdownDaysShowDaysAndHours() {
        // 6d22h
        XCTAssertEqual(Fmt.countdown(to: plus(6 * 86400 + 22 * 3600), now: now), "6d22h")
        // exactly one day
        XCTAssertEqual(Fmt.countdown(to: plus(86400), now: now), "1d0h")
    }

    func testCountdownFarOutRendersDate() {
        let far = plus(9 * 86400)
        let label = Fmt.countdown(to: far, now: now)
        // Should be a "MMM d" date, not a d/h countdown.
        XCTAssertFalse(label.contains("d") && label.contains("h"))
        XCTAssertEqual(label, Fmt.dateLabel(far))
    }

    func testCountdownPastIsZeroMinutes() {
        XCTAssertEqual(Fmt.countdown(to: plus(-500), now: now), "0m")
    }

    // MARK: Consumed fraction math

    func testConsumedFraction() {
        XCTAssertEqual(Fmt.consumed(pctLeft: 100), 0.0, accuracy: 0.0001)
        XCTAssertEqual(Fmt.consumed(pctLeft: 74), 0.26, accuracy: 0.0001)
        XCTAssertEqual(Fmt.consumed(pctLeft: 25), 0.75, accuracy: 0.0001)
        XCTAssertEqual(Fmt.consumed(pctLeft: 0), 1.0, accuracy: 0.0001)
    }

    func testConsumedFractionClampsAndHandlesNull() {
        XCTAssertEqual(Fmt.consumed(pctLeft: nil), 0.0, accuracy: 0.0001)
        XCTAssertEqual(Fmt.consumed(pctLeft: 150), 0.0, accuracy: 0.0001)
        XCTAssertEqual(Fmt.consumed(pctLeft: -20), 1.0, accuracy: 0.0001)
    }

    // MARK: Remaining fraction (glance fuel bar)

    func testRemainingFractionMatchesItsNumber() {
        // The glance bar fills by what's LEFT, the inverse of `consumed`.
        XCTAssertEqual(Fmt.remaining(pctLeft: 100), 1.0, accuracy: 0.0001)
        XCTAssertEqual(Fmt.remaining(pctLeft: 74), 0.74, accuracy: 0.0001)
        XCTAssertEqual(Fmt.remaining(pctLeft: 18), 0.18, accuracy: 0.0001)
        XCTAssertEqual(Fmt.remaining(pctLeft: 0), 0.0, accuracy: 0.0001)
    }

    func testRemainingFractionClampsAndHandlesNull() {
        XCTAssertEqual(Fmt.remaining(pctLeft: nil), 0.0, accuracy: 0.0001)
        XCTAssertEqual(Fmt.remaining(pctLeft: 150), 1.0, accuracy: 0.0001)
        XCTAssertEqual(Fmt.remaining(pctLeft: -20), 0.0, accuracy: 0.0001)
    }

    func testGlancePercentGlyph() {
        XCTAssertEqual(Fmt.glancePercent(pctLeft: 18), "18")
        XCTAssertEqual(Fmt.glancePercent(pctLeft: 0), "0")
        XCTAssertEqual(Fmt.glancePercent(pctLeft: nil), "--")
    }

    // MARK: Severity color mapping

    func testSeverityMapping() {
        XCTAssertEqual(Theme.severity(pctLeft: 80), Theme.green)
        XCTAssertEqual(Theme.severity(pctLeft: 25), Theme.green) // boundary: <25 is amber, 25 is green
        XCTAssertEqual(Theme.severity(pctLeft: 24), Theme.amber)
        XCTAssertEqual(Theme.severity(pctLeft: 1), Theme.amber)
        XCTAssertEqual(Theme.severity(pctLeft: 0), Theme.red)
        XCTAssertEqual(Theme.severity(pctLeft: nil), Theme.hairline)
    }

    // MARK: Meter value + window labels

    func testMeterValueComposition() {
        XCTAssertEqual(
            Fmt.meterValue(pctLeft: 74, resetsAt: plus(72 * 60), now: now),
            "74% · 1h12"
        )
        XCTAssertEqual(Fmt.meterValue(pctLeft: 43, resetsAt: nil, now: now), "43%")
        XCTAssertEqual(Fmt.meterValue(pctLeft: nil, resetsAt: nil, now: now), "--%")
    }

    func testWindowLabels() {
        XCTAssertEqual(Fmt.windowLabel(kind: "session_5h"), "5h")
        XCTAssertEqual(Fmt.windowLabel(kind: "weekly_7d"), "7d")
        XCTAssertEqual(Fmt.windowLabel(kind: "weekly_fable"), "F")
        XCTAssertEqual(Fmt.windowLabel(kind: "weekly"), "7d")
    }

    // MARK: Value formatting

    func testUSDFormatting() {
        XCTAssertEqual(Fmt.usd(182), "$182")
        XCTAssertEqual(Fmt.usd(3140), "$3.1k")
        XCTAssertEqual(Fmt.usd(0), "$0")
    }

    func testMultipleFormatting() {
        XCTAssertEqual(Fmt.multiple(12.6), "12.6×")
    }

    func testSinceLabel() {
        XCTAssertEqual(Fmt.sinceLabel(seconds: 44), "44s")
        XCTAssertEqual(Fmt.sinceLabel(seconds: 72), "1m12")
        XCTAssertEqual(Fmt.sinceLabel(seconds: 3 * 3600 + 5 * 60), "3h05")
        XCTAssertEqual(Fmt.sinceLabel(seconds: nil), "")
    }

    // MARK: Cluster opacity derivations

    func testClusterOpacity() {
        let working = Subscription(id: "a", provider: "claude", label: "A",
            windows: [Window(kind: "session_5h", pctLeft: 50, resetsAt: nil, pace: nil)],
            tightest: nil, stale: nil, activeAgents: 2)
        XCTAssertEqual(working.clusterOpacity, 1.0, accuracy: 0.001)

        let idle = Subscription(id: "b", provider: "claude", label: "B",
            windows: [Window(kind: "session_5h", pctLeft: 50, resetsAt: nil, pace: nil)],
            tightest: nil, stale: nil, activeAgents: 0)
        XCTAssertEqual(idle.clusterOpacity, 0.4, accuracy: 0.001)

        let spent = Subscription(id: "c", provider: "claude", label: "C",
            windows: [Window(kind: "session_5h", pctLeft: 0, resetsAt: nil, pace: nil)],
            tightest: nil, stale: nil, activeAgents: 0)
        XCTAssertEqual(spent.clusterOpacity, 0.55, accuracy: 0.001)
    }
}
