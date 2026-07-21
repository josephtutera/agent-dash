import SwiftUI

// Both brand marks are drawn from scratch in SwiftUI, no bitmaps, so they stay
// crisp at any scale and pick up the theme tint.

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

/// The OpenAI/Codex "knot": six rounded-rect arms placed 60° apart, each offset
/// out from the center, in white.
public struct OpenAIKnot: View {
    public var size: CGFloat = 13
    public var color: Color = .white
    public init(size: CGFloat = 13, color: Color = .white) {
        self.size = size
        self.color = color
    }

    public var body: some View {
        Canvas { context, canvasSize in
            let center = CGPoint(x: canvasSize.width / 2, y: canvasSize.height / 2)
            let radius = min(canvasSize.width, canvasSize.height) / 2
            let armLength = radius * 1.15
            let armWidth = radius * 0.42
            let offset = radius * 0.30
            for i in 0..<6 {
                let angle = Double(i) * (.pi / 3.0) // 60 degrees
                var arm = Path(
                    roundedRect: CGRect(
                        x: -armWidth / 2,
                        y: -armLength / 2 + offset,
                        width: armWidth,
                        height: armLength
                    ),
                    cornerRadius: armWidth / 2
                )
                let transform = CGAffineTransform(translationX: center.x, y: center.y)
                    .rotated(by: angle)
                arm = arm.applying(transform)
                context.fill(arm, with: .color(color))
            }
        }
        .frame(width: size, height: size)
    }
}

/// Chooses the right mark for a provider.
public struct BrandMark: View {
    public let provider: String
    public var size: CGFloat = 13
    public init(provider: String, size: CGFloat = 13) {
        self.provider = provider
        self.size = size
    }
    public var body: some View {
        if provider == "codex" {
            OpenAIKnot(size: size)
        } else {
            ClaudeSpark(size: size)
        }
    }
}
