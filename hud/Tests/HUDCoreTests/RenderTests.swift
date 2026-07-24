import XCTest
import SwiftUI
@testable import HUDCore

/// Headless smoke test: the whole card view tree must lay out and rasterize
/// without crashing, in both appearances. Renders the sample snapshot to a temp
/// PNG and checks the file is a non-trivial image. The committed hud/preview.png
/// is generated the same way via `swift run adash-hud --render-preview`.
final class RenderTests: XCTestCase {

    private func assertNonEmptyPNG(at out: URL) throws {
        let data = try Data(contentsOf: out)
        XCTAssertGreaterThan(data.count, 2000, "rendered PNG suspiciously small")
        // PNG magic number.
        XCTAssertEqual(Array(data.prefix(4)), [0x89, 0x50, 0x4E, 0x47])
    }

    @MainActor
    func testRendersCardToNonEmptyPNG() throws {
        let out = FileManager.default.temporaryDirectory
            .appendingPathComponent("adash-hud-test-\(UUID().uuidString).png")
        defer { try? FileManager.default.removeItem(at: out) }

        try PreviewRenderer.renderCardPNG(to: out, scale: 2, colorScheme: .dark)
        try assertNonEmptyPNG(at: out)
    }

    @MainActor
    func testRendersLightCardToNonEmptyPNG() throws {
        let out = FileManager.default.temporaryDirectory
            .appendingPathComponent("adash-hud-light-test-\(UUID().uuidString).png")
        defer { try? FileManager.default.removeItem(at: out) }

        try PreviewRenderer.renderCardPNG(to: out, scale: 2, colorScheme: .light)
        try assertNonEmptyPNG(at: out)
    }
}
