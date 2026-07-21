import Foundation
import CoreGraphics

/// Which face the HUD should wear, decided purely from the current display
/// arrangement so it can be unit tested without AppKit.
public enum HUDMode: Equatable {
    /// Draw the notch pill on the built-in display.
    case notch
    /// Fall back to the NSStatusItem.
    case menubar
}

/// A minimal description of one attached display.
public struct DisplayInfo: Equatable {
    public let isBuiltIn: Bool
    /// Top safe-area inset; > 0 means the display has a camera housing.
    public let notchHeight: CGFloat
    /// Width of the camera housing, 0 when there is none.
    public let notchWidth: CGFloat

    public init(isBuiltIn: Bool, notchHeight: CGFloat, notchWidth: CGFloat) {
        self.isBuiltIn = isBuiltIn
        self.notchHeight = notchHeight
        self.notchWidth = notchWidth
    }
}

public enum ModePolicy {
    /// The display rule from the design brief: notch face only when the
    /// built-in display is the sole display and actually has a notch. Any
    /// external display (docked or mirrored-off) or clamshell arrangement
    /// gets the menubar item instead, and a fake notch is never drawn.
    public static func mode(for displays: [DisplayInfo]) -> HUDMode {
        guard displays.count == 1,
              let only = displays.first,
              only.isBuiltIn,
              only.notchHeight > 0
        else { return .menubar }
        return .notch
    }
}
