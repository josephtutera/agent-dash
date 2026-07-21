import SwiftUI

/// The collapsed notch face, redesigned to sit *beside* the camera housing
/// rather than straddle it: the full three-cluster strip was wider than a
/// physical notch and spilled onto the app's Window/Help menus. Collapsed now
/// carries only what you need at a glance — one colored dot per running agent
/// (spinning while it works), the launcher chip, and a single severity ring for
/// the tightest quota so a plan running dry still warns. The full breakdown
/// (all three subscriptions, value, reset times) drops in on hover.
public struct NotchFaceView: View {
    public let snapshot: HUDSnapshot?
    public var now: Date
    /// Menubar/notch height on this display.
    public var height: CGFloat
    public var onSelect: ((Agent) -> Void)?
    public var onLaunch: (() -> Void)?

    public init(
        snapshot: HUDSnapshot?,
        now: Date = Date(),
        height: CGFloat = 34,
        onSelect: ((Agent) -> Void)? = nil,
        onLaunch: (() -> Void)? = nil
    ) {
        self.snapshot = snapshot
        self.now = now
        self.height = height
        self.onSelect = onSelect
        self.onLaunch = onLaunch
    }

    public static let cornerRadius: CGFloat = 14

    public var body: some View {
        HStack(spacing: 12) {
            AgentClusterView(
                agents: snapshot?.agents ?? [],
                diameter: 18,
                onSelect: onSelect,
                onLaunch: onLaunch
            )
            // A single severity ring for the tightest quota across all plans,
            // so a plan burning down still reads without opening the card.
            if let worst = snapshot?.worstWindow {
                RingCluster(rings: [worst], diameter: 16, strokeWidth: 2)
            }
        }
        .padding(.horizontal, 14)
        .frame(height: height)
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

/// The hover/expanded state: the compact face stays put and the full card hangs
/// directly below it. This is the only place the three subscription clusters,
/// value, and reset times appear now.
public struct NotchExpandedView: View {
    public let snapshot: HUDSnapshot?
    public var now: Date
    public var faceHeight: CGFloat
    public var onSelect: ((Agent) -> Void)?
    public var onLaunch: (() -> Void)?

    public init(
        snapshot: HUDSnapshot?,
        now: Date = Date(),
        faceHeight: CGFloat = 34,
        onSelect: ((Agent) -> Void)? = nil,
        onLaunch: (() -> Void)? = nil
    ) {
        self.snapshot = snapshot
        self.now = now
        self.faceHeight = faceHeight
        self.onSelect = onSelect
        self.onLaunch = onLaunch
    }

    public var body: some View {
        VStack(spacing: 6) {
            NotchFaceView(
                snapshot: snapshot,
                now: now,
                height: faceHeight,
                onSelect: onSelect,
                onLaunch: onLaunch
            )
            PopoverCard(snapshot: snapshot, now: now)
        }
    }
}
