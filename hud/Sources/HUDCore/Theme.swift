import SwiftUI

// The single source of truth for the instrument-cluster palette and type ramp.
// Never inline a hex or a font size at a call site; reach for a token here.
public enum Theme {

    // MARK: Colors (from the approved Paper artboards)
    public static let notch    = Color(hex: 0x000000)
    public static let panel    = Color(hex: 0x101114)
    public static let panel2   = Color(hex: 0x17181C)
    public static let hairline = Color(hex: 0x2A2C33)
    public static let text     = Color(hex: 0xF2F3F5)
    public static let muted    = Color(hex: 0x8A8F98)
    public static let amber    = Color(hex: 0xFFB340)
    public static let green    = Color(hex: 0x34C759)
    public static let red      = Color(hex: 0xFF5F57)

    // Brand marks
    public static let claudeCoral = Color(hex: 0xD97757)
    public static let codexGreen  = Color(hex: 0x19C37D)

    // MARK: Severity
    /// Ring / meter color driven by percent remaining.
    /// Fully spent (0) reads red, pressured (<25) amber, otherwise green.
    /// A null reading has no severity and renders as the track color.
    public static func severity(pctLeft: Int?) -> Color {
        guard let pct = pctLeft else { return hairline }
        if pct <= 0 { return red }
        if pct < 25 { return amber }
        return green
    }

    /// The tool dot color used in the agents section.
    public static func toolColor(_ tool: String) -> Color {
        switch tool {
        case "claude":   return claudeCoral
        case "codex":    return codexGreen
        case "opencode": return Color(hex: 0xA78BFA)
        default:         return muted
        }
    }

    /// The brand mark tint for a subscription provider.
    public static func providerColor(_ provider: String) -> Color {
        provider == "codex" ? Color.white : claudeCoral
    }

    // MARK: Type
    public static func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }

    public static func label(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .default)
    }
}

extension Color {
    /// Construct an opaque color from a packed 0xRRGGBB integer.
    public init(hex: UInt32) {
        let r = Double((hex >> 16) & 0xFF) / 255.0
        let g = Double((hex >> 8) & 0xFF) / 255.0
        let b = Double(hex & 0xFF) / 255.0
        self = Color(.sRGB, red: r, green: g, blue: b, opacity: 1.0)
    }
}
