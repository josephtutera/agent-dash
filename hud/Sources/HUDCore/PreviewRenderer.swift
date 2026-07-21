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

        return try write(content, to: url, scale: scale, opaque: true)
    }

    /// Renders the menu-bar glance (status-item content) beside its dropdown
    /// card over a desktop-gray backdrop, so reviewers see both the collapsed
    /// glance and the click-through card in one image.
    @discardableResult
    public static func renderMenubarPNG(
        snapshot: HUDSnapshot = .sample,
        now: Date = HUDSnapshot.previewNow,
        to url: URL,
        scale: CGFloat = 2
    ) throws -> URL {
        let content = VStack(alignment: .trailing, spacing: 24) {
            // The glance on a dark menu-bar strip, right-aligned like the real
            // status area.
            HStack {
                Spacer()
                // White mimics AppKit's template tint on a dark menu bar.
                MenuBarContentView(snapshot: snapshot, now: now, tint: .white)
            }
            .padding(.horizontal, 12)
            .frame(height: 28)
            .background(Color(hex: 0x26272C)) // --color-menubar from the artboards

            PopoverCard(snapshot: snapshot, now: now)
        }
        .padding(40)
        .background(Color(hex: 0x1C1D21)) // --color-desktop from the artboards

        return try write(content, to: url, scale: scale, opaque: true)
    }

    private static func write(
        _ content: some View,
        to url: URL,
        scale: CGFloat,
        opaque: Bool
    ) throws -> URL {
        let renderer = ImageRenderer(content: content)
        renderer.scale = scale
        renderer.isOpaque = opaque

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
