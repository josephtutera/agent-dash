import XCTest
@testable import HUDCore

final class DecodingTests: XCTestCase {

    private func loadFixtureData() throws -> Data {
        guard let url = Bundle.module.url(forResource: "snapshot", withExtension: "json") else {
            XCTFail("fixture snapshot.json not found in test bundle")
            return Data()
        }
        return try Data(contentsOf: url)
    }

    func testDecodesFixtureSnapshot() throws {
        let snap = try HUDSnapshot.decode(from: loadFixtureData())

        XCTAssertEqual(snap.version, 1)
        XCTAssertNotNil(snap.generatedAt)
        XCTAssertEqual(snap.subscriptions.count, 3)
        XCTAssertEqual(snap.agents.count, 3)
        XCTAssertNotNil(snap.value)
        XCTAssertNotNil(snap.soonestReset)
    }

    func testSubscriptionFieldsRoundTrip() throws {
        let snap = try HUDSnapshot.decode(from: loadFixtureData())

        let team = try XCTUnwrap(snap.subscriptions.first { $0.id == "claude-team" })
        XCTAssertEqual(team.provider, "claude")
        XCTAssertEqual(team.label, "Claude Team")
        XCTAssertEqual(team.activeAgents, 2)
        XCTAssertNil(team.stale)
        XCTAssertEqual(team.windows.count, 2)

        let session = try XCTUnwrap(team.sessionWindow)
        XCTAssertEqual(session.pctLeft, 18)
        XCTAssertNotNil(session.pace)
        XCTAssertEqual(session.pace?.marginSeconds, 5100)

        let weekly = try XCTUnwrap(team.weeklyWindow)
        XCTAssertEqual(weekly.kind, "weekly_7d")
        XCTAssertNil(weekly.pace)
    }

    func testNullAndZeroFieldsDecode() throws {
        let snap = try HUDSnapshot.decode(from: loadFixtureData())

        let personal = try XCTUnwrap(snap.subscriptions.first { $0.id == "claude-personal" })
        let fable = try XCTUnwrap(personal.windows.first { $0.kind == "weekly_fable" })
        XCTAssertEqual(fable.pctLeft, 0)
        XCTAssertTrue(fable.isLimitReached)
        XCTAssertTrue(personal.hasLimitReached)
        XCTAssertEqual(personal.activeAgents, 0)
    }

    func testValueBlockDecodes() throws {
        let snap = try HUDSnapshot.decode(from: loadFixtureData())
        let value = try XCTUnwrap(snap.value)
        XCTAssertEqual(value.todayUSD, 182.0, accuracy: 0.001)
        XCTAssertEqual(value.monthUSD, 3140.0, accuracy: 0.001)
        XCTAssertEqual(value.subsCostUSD ?? -1, 250.0, accuracy: 0.001)
        XCTAssertEqual(value.multiple ?? -1, 12.6, accuracy: 0.001)
        XCTAssertEqual(value.bySub.count, 3)
        XCTAssertEqual(value.bySub["claude-team"]?.monthUSD ?? -1, 2100.0, accuracy: 0.001)
    }

    func testAgentsDecode() throws {
        let snap = try HUDSnapshot.decode(from: loadFixtureData())
        XCTAssertEqual(snap.waitingAgentCount, 1)
        let waiting = try XCTUnwrap(snap.agents.first { $0.isWaiting })
        XCTAssertEqual(waiting.project, "prototype-ehr")
        XCTAssertEqual(waiting.tool, "claude")
        XCTAssertEqual(waiting.sinceSeconds, 72)
    }

    func testDecoderToleratesPlainAndFractionalDates() throws {
        // A plain ISO8601 (no fractional seconds) and a fractional one both parse.
        let plain = #"{"version":1,"generated_at":"2026-07-21T13:41:00Z","subscriptions":[],"agents":[],"value":null,"soonest_reset":null}"#
        let frac  = #"{"version":1,"generated_at":"2026-07-21T13:41:00.500Z","subscriptions":[],"agents":[],"value":null,"soonest_reset":null}"#
        XCTAssertNotNil(try HUDSnapshot.decode(from: Data(plain.utf8)).generatedAt)
        XCTAssertNotNil(try HUDSnapshot.decode(from: Data(frac.utf8)).generatedAt)
    }
}
