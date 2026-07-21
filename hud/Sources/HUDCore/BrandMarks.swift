import SwiftUI
#if canImport(AppKit)
import AppKit
#endif

// The Claude spark is drawn from scratch in SwiftUI so it stays crisp at any
// scale; the OpenAI mark is the real logo (an embedded template image). Both
// are tintable so they render in the card's brand colors or, in the menu bar,
// in a single monochrome tint that AppKit re-colors for guaranteed contrast.

/// The Claude "spark": twelve short lines radiating from the center at 30°
/// steps, round-capped, in coral.
public struct ClaudeSpark: View {
    public var size: CGFloat = 13
    public var color: Color = Theme.claudeCoral
    public init(size: CGFloat = 13, color: Color = Theme.claudeCoral) {
        self.size = size
        self.color = color
    }

    public var body: some View {
        Canvas { context, canvasSize in
            let center = CGPoint(x: canvasSize.width / 2, y: canvasSize.height / 2)
            let outer = min(canvasSize.width, canvasSize.height) / 2
            let inner = outer * 0.34
            let lineWidth = max(1.2, outer * 0.16)
            for i in 0..<12 {
                let angle = Double(i) * (.pi / 6.0) // 30 degrees
                let dx = cos(angle), dy = sin(angle)
                var path = Path()
                path.move(to: CGPoint(x: center.x + dx * inner, y: center.y + dy * inner))
                path.addLine(to: CGPoint(x: center.x + dx * outer, y: center.y + dy * outer))
                context.stroke(
                    path,
                    with: .color(color),
                    style: StrokeStyle(lineWidth: lineWidth, lineCap: .round)
                )
            }
        }
        .frame(width: size, height: size)
    }
}

/// The OpenAI/Codex mark: the real interwoven "blossom" logo, drawn as a SwiftUI
/// template image so `color` fully determines its fill (white on the card, or a
/// monochrome tint in the menu bar). Decoded once from the embedded asset.
public struct OpenAIMark: View {
    public var size: CGFloat = 13
    public var color: Color = .white
    public init(size: CGFloat = 13, color: Color = .white) {
        self.size = size
        self.color = color
    }

    public var body: some View {
        #if canImport(AppKit)
        Image(nsImage: Self.templateImage)
            .resizable()
            .renderingMode(.template)
            .interpolation(.high)
            .aspectRatio(contentMode: .fit)
            .foregroundStyle(color)
            .frame(width: size, height: size)
        #else
        Color.clear.frame(width: size, height: size)
        #endif
    }

    #if canImport(AppKit)
    /// The embedded PNG mask, decoded once and flagged as a template so it takes
    /// the foreground tint (and AppKit's menu-bar tint) rather than its own pixels.
    static let templateImage: NSImage = {
        let data = Data(base64Encoded: OpenAIMarkAsset.pngBase64, options: .ignoreUnknownCharacters) ?? Data()
        let image = NSImage(data: data) ?? NSImage(size: NSSize(width: 1, height: 1))
        image.isTemplate = true
        return image
    }()
    #endif
}

/// Chooses the right mark for a provider. `tint` overrides the brand color with
/// a single monochrome color (used by the menu-bar glance, which AppKit then
/// re-tints for contrast); when nil each provider draws in its own brand color.
public struct BrandMark: View {
    public let provider: String
    public var size: CGFloat = 13
    public var tint: Color?
    public init(provider: String, size: CGFloat = 13, tint: Color? = nil) {
        self.provider = provider
        self.size = size
        self.tint = tint
    }
    public var body: some View {
        if provider == "codex" {
            OpenAIMark(size: size, color: tint ?? .white)
        } else {
            ClaudeSpark(size: size, color: tint ?? Theme.claudeCoral)
        }
    }
}
