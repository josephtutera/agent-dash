import SwiftUI

/// One concentric ring cluster, outermost ring first. Each ring fills by the
/// consumed fraction of its window, colored by severity, drawn round-capped
/// over a hairline track. A fully spent limit therefore renders as a complete
/// solid ring. Now used only by the popover's offline state (three dim rings);
/// the menu-bar glance shows percentages and a fuel bar, not rings.
public struct RingCluster: View {
    public let rings: [Window?]
    public var diameter: CGFloat = 18
    public var strokeWidth: CGFloat = 2
    /// When set, the cluster renders monochrome in this single color (faint
    /// track, solid fill) instead of the severity palette — for the menu-bar
    /// glance, which AppKit re-tints for contrast. Fill level still shows how
    /// spent a limit is; the color-coded severity lives in the full card.
    public var tint: Color?

    public init(rings: [Window?], diameter: CGFloat = 18, strokeWidth: CGFloat = 2, tint: Color? = nil) {
        self.rings = rings
        self.diameter = diameter
        self.strokeWidth = strokeWidth
        self.tint = tint
    }

    /// The original two-ring form used by the menubar mini and offline states.
    public init(session: Window?, weekly: Window?, diameter: CGFloat = 18, strokeWidth: CGFloat = 2, tint: Color? = nil) {
        self.init(rings: [session, weekly], diameter: diameter, strokeWidth: strokeWidth, tint: tint)
    }

    public var body: some View {
        ZStack {
            ForEach(Array(rings.enumerated()), id: \.offset) { index, window in
                ring(for: window, inset: inset(at: index))
            }
        }
        .frame(width: diameter, height: diameter)
    }

    /// Ring spacing from the approved artboards: at stroke 2 the radii step by
    /// 3pt per ring (26pt cluster: r 11/8/5; 18pt cluster: r 7/4), i.e. one
    /// stroke off the frame edge, then a stroke-and-a-point per level inward.
    private func inset(at index: Int) -> CGFloat {
        strokeWidth + CGFloat(index) * (strokeWidth + 1)
    }

    @ViewBuilder
    private func ring(for window: Window?, inset: CGFloat) -> some View {
        let fraction = Fmt.consumed(pctLeft: window?.pctLeft)
        let trackColor = tint.map { $0.opacity(0.32) } ?? Theme.hairline
        let fillColor = tint ?? Theme.severity(pctLeft: window?.pctLeft)
        ZStack {
            Circle()
                .stroke(trackColor, lineWidth: strokeWidth)
            Circle()
                .trim(from: 0, to: max(0, min(1, fraction)))
                .stroke(
                    fillColor,
                    style: StrokeStyle(lineWidth: strokeWidth, lineCap: .round)
                )
                .rotationEffect(.degrees(-90))
        }
        .padding(inset)
    }
}
