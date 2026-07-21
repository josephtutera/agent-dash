import SwiftUI

/// The collapsed notch pill from artboard "1 · Collapsed (rings)": a black
/// blob that hugs the camera housing, with the three ring clusters on the
/// left flank, the soonest-reset countdown on the right flank, and rounded
/// bottom corners so it reads as one shape with the physical notch. The
/// camera gap in the middle is sized to the real notch at runtime (the
/// preview render uses a stand-in width).
public struct NotchFaceView: View {
    public let snapshot: HUDSnapshot?
    public var now: Date
    /// Width of the physical camera housing the flanks wrap around.
    public var cameraWidth: CGFloat
    /// Menubar/notch height on this display.
    public var height: CGFloat

    public init(
        snapshot: HUDSnapshot?,
        now: Date = Date(),
        cameraWidth: CGFloat = 160,
        height: CGFloat = 34
    ) {
        self.snapshot = snapshot
        self.now = now
        self.cameraWidth = cameraWidth
        self.height = height
    }

    /// Each flank's width, from the approved artboard (150pt at 16pt padding).
    public static let flankWidth: CGFloat = 150
    public static let cornerRadius: CGFloat = 14

    public var totalWidth: CGFloat { cameraWidth + 2 * Self.flankWidth }

    private static let order = ["claude-team", "claude-personal", "codex"]

    private var orderedSubs: [Subscription] {
        guard let snap = snapshot else { return [] }
        return Self.order.compactMap { id in snap.subscriptions.first { $0.id == id } }
    }

    public var body: some View {
        HStack(spacing: 0) {
            // Left flank: one 26pt three-ring cluster per subscription,
            // brightness carrying the agents-running signal.
            HStack(spacing: 12) {
                if !orderedSubs.isEmpty {
                    ForEach(orderedSubs) { sub in
                        RingCluster(rings: sub.notchRings, diameter: 26, strokeWidth: 2)
                            .opacity(sub.clusterOpacity)
                    }
                } else {
                    ForEach(0..<3, id: \.self) { _ in
                        RingCluster(rings: [nil, nil, nil], diameter: 26, strokeWidth: 2)
                            .opacity(0.35)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.leading, 16)
            .frame(width: Self.flankWidth)

            // Camera housing gap. The dot is a subtle hint in previews; on the
            // real display it sits behind the physical camera.
            ZStack {
                Circle()
                    .fill(Color(hex: 0x121316))
                    .frame(width: 10, height: 10)
            }
            .frame(width: cameraWidth)

            // Right flank: the soonest-reset countdown, right-aligned.
            HStack(spacing: 10) {
                Spacer(minLength: 0)
                if let soonest = snapshot?.soonestReset {
                    Text(Fmt.countdown(to: soonest.resetsAt, now: now))
                        .font(Theme.mono(12, weight: .medium))
                        .foregroundStyle(Theme.amber)
                        .monospacedDigit()
                }
            }
            .padding(.trailing, 16)
            .frame(width: Self.flankWidth)
        }
        .frame(width: totalWidth, height: height)
        .background(
            UnevenRoundedRectangle(
                cornerRadii: .init(
                    bottomLeading: Self.cornerRadius,
                    bottomTrailing: Self.cornerRadius
                ),
                style: .continuous
            )
            .fill(Theme.notch)
        )
    }
}

/// The hover/expanded state from artboard "2 · Expanded (hover)": the notch
/// pill stays put and the full card hangs directly below it, centered.
public struct NotchExpandedView: View {
    public let snapshot: HUDSnapshot?
    public var now: Date
    public var cameraWidth: CGFloat
    public var faceHeight: CGFloat

    public init(
        snapshot: HUDSnapshot?,
        now: Date = Date(),
        cameraWidth: CGFloat = 160,
        faceHeight: CGFloat = 34
    ) {
        self.snapshot = snapshot
        self.now = now
        self.cameraWidth = cameraWidth
        self.faceHeight = faceHeight
    }

    public var body: some View {
        VStack(spacing: 6) {
            NotchFaceView(
                snapshot: snapshot,
                now: now,
                cameraWidth: cameraWidth,
                height: faceHeight
            )
            PopoverCard(snapshot: snapshot, now: now)
        }
    }
}
