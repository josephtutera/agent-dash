import SwiftUI

#if canImport(AppKit)
import AppKit
import UniformTypeIdentifiers

/// Renders the popover card with a fixed snapshot to a PNG at a given scale,
/// using SwiftUI's ImageRenderer. This is the evidence artifact reviewers look
/// at, and it doubles as a headless smoke test that the whole view tree lays
/// out without crashing.
@MainActor
public enum PreviewRenderer {

    public struct RenderError: Error, CustomStringConvertible {
        public let description: String
    }

    @discardableResult
    public static func renderCardPNG(
        snapshot: HUDSnapshot = .sample,
        now: Date = HUDSnapshot.previewNow,
        to url: URL,
        scale: CGFloat = 2
    ) throws -> URL {
        // A little breathing room around the card so the border isn't flush
        // to the image edge, over the notch-black backdrop.
        let content = PopoverCard(snapshot: snapshot, now: now)
            .padding(20)
            .background(Theme.notch)

        let renderer = ImageRenderer(content: content)
        renderer.scale = scale
        renderer.isOpaque = true

        guard let cgImage = renderer.cgImage else {
            throw RenderError(description: "ImageRenderer produced no image")
        }

        guard let dest = CGImageDestinationCreateWithURL(
            url as CFURL, UTType.png.identifier as CFString, 1, nil
        ) else {
            throw RenderError(description: "could not create PNG destination at \(url.path)")
        }
        CGImageDestinationAddImage(dest, cgImage, nil)
        guard CGImageDestinationFinalize(dest) else {
            throw RenderError(description: "could not finalize PNG at \(url.path)")
        }
        return url
    }
}
#endif
