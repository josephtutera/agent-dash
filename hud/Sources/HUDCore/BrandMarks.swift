import SwiftUI
#if canImport(AppKit)
import AppKit
#endif

// Both brand marks are the real vendor logos, embedded as black-on-transparent
// PNGs (see ClaudeMarkAsset / OpenAIMarkAsset) and drawn as SwiftUI *template*
// images so `color` fully determines the fill: the brand color on the card, or a
// single monochrome tint in the menu bar, which AppKit re-colors for guaranteed
// contrast over any wallpaper.

#if canImport(AppKit)
/// Decode a base64 PNG into a template NSImage. The template flag means the
/// image takes the foreground/menu-bar tint rather than its own pixels, so only
/// its alpha (the logo silhouette) matters.
func decodeTemplateImage(fromBase64 base64: String) -> NSImage {
    let data = Data(base64Encoded: base64, options: .ignoreUnknownCharacters) ?? Data()
    let image = NSImage(data: data) ?? NSImage(size: NSSize(width: 1, height: 1))
    image.isTemplate = true
    return image
}
#endif

/// The Claude "starburst" logo, drawn as a SwiftUI template image so `color`
/// fully determines its fill (coral on the card, or a monochrome tint in the
/// menu bar). Decoded once from the embedded asset.
public struct ClaudeMark: View {
    public var size: CGFloat = 13
    public var color: Color = Theme.claudeCoral
    public init(size: CGFloat = 13, color: Color = Theme.claudeCoral) {
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
    static let templateImage: NSImage = decodeTemplateImage(fromBase64: ClaudeMarkAsset.pngBase64)
    #endif
}

/// The OpenAI/Codex "blossom" logo, drawn as a SwiftUI template image so `color`
/// fully determines its fill (white on the card, or a monochrome tint in the
/// menu bar). Decoded once from the embedded asset.
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
    static let templateImage: NSImage = decodeTemplateImage(fromBase64: OpenAIMarkAsset.pngBase64)
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
            ClaudeMark(size: size, color: tint ?? Theme.claudeCoral)
        }
    }
}
