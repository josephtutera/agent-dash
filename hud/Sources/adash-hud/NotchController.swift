import AppKit
import SwiftUI
import HUDCore

/// Owns the notch-hugging panel: a borderless, non-activating window pinned
/// over the camera housing of the built-in display. Collapsed it is exactly
/// the pill (so clicks on the rest of the menubar are never stolen); on hover
/// it grows downward to hang the full card below the notch, and shrinks back
/// on mouse exit.
///
/// The panel deliberately does NOT set `.fullScreenAuxiliary`, so it stays out
/// of fullscreen spaces entirely; the menubar is hidden there and a floating
/// pill over someone's movie would be worse than absence.
@MainActor
final class NotchController {
    private let store: HUDStore
    private var panel: NSPanel?
    private var hosting: NSHostingView<AnyView>?
    private let state = NotchViewState()
    private weak var screen: NSScreen?

    init(store: HUDStore) {
        self.store = store
    }

    var isShowing: Bool { panel != nil }

    /// Builds (or rebuilds) the panel on the given screen. Call `hide()` first
    /// when the display arrangement changed; windows are rebuilt, not moved,
    /// because AppKit panels get confused when their screen disappears.
    func show(on screen: NSScreen) {
        hide()
        self.screen = screen

        let geometry = NotchGeometry(screen: screen)
        state.cameraWidth = geometry.cameraWidth
        state.faceHeight = geometry.faceHeight
        state.onHoverChange = { [weak self] hovering in
            self?.setExpanded(hovering)
        }

        let root = NotchRoot(state: state).environmentObject(store)
        let hosting = NSHostingView(rootView: AnyView(root))
        self.hosting = hosting

        let panel = NSPanel(
            contentRect: .zero,
            styleMask: [.nonactivatingPanel, .borderless, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        panel.contentView = hosting
        panel.isFloatingPanel = true
        panel.level = .statusBar
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.isMovable = false
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        self.panel = panel

        applyFrame(expanded: false)
        panel.orderFrontRegardless()
    }

    func hide() {
        panel?.orderOut(nil)
        panel = nil
        hosting = nil
        screen = nil
        state.expanded = false
    }

    private func setExpanded(_ expanded: Bool) {
        guard state.expanded != expanded else { return }
        state.expanded = expanded
        applyFrame(expanded: expanded)
    }

    /// Sizes the panel to exactly the drawn content: the bare pill when
    /// collapsed, pill-plus-card when expanded, both top-centered on the notch.
    private func applyFrame(expanded: Bool) {
        guard let panel, let hosting, let screen else { return }
        hosting.layout()
        let size = hosting.fittingSize
        let width = max(size.width, 1)
        let height = max(size.height, 1)
        // Anchor just right of the physical notch so the pill never sits over
        // the app's left-hand menus (Window/Help). Clamp so the (wider) hover
        // card can't run off the right edge of the display.
        let notchRightEdge = screen.frame.midX + state.cameraWidth / 2
        let gap: CGFloat = 6
        let x = min(notchRightEdge + gap, screen.frame.maxX - width - 8)
        let frame = NSRect(
            x: max(screen.frame.minX + 8, x),
            y: screen.frame.maxY - height,
            width: width,
            height: height
        )
        panel.setFrame(frame, display: true)
        _ = expanded
    }
}

/// Observable UI state shared between the controller and the SwiftUI tree.
@MainActor
final class NotchViewState: ObservableObject {
    @Published var expanded = false
    var cameraWidth: CGFloat = 160
    var faceHeight: CGFloat = 34
    var onHoverChange: ((Bool) -> Void)?
}

/// The SwiftUI content of the notch panel: the pill always, the card only
/// while hovered. Top-aligned so the pill never moves as the card appears.
private struct NotchRoot: View {
    @ObservedObject var state: NotchViewState
    @EnvironmentObject var store: HUDStore

    var body: some View {
        // Leading-aligned so the compact face stays pinned to the same spot
        // (right of the notch) whether or not the wider card is showing, instead
        // of jumping as the panel grows.
        VStack(alignment: .leading, spacing: 6) {
            NotchFaceView(
                snapshot: store.snapshot,
                now: store.now,
                height: state.faceHeight,
                onSelect: { AppActions.jumpToAgent($0) },
                onLaunch: { AppActions.openLauncher() }
            )

            if state.expanded {
                PopoverCard(snapshot: store.snapshot, now: store.now)
                    .shadow(color: .black.opacity(0.55), radius: 30, y: 24)
            }
        }
        .contentShape(Rectangle())
        .onHover { hovering in
            state.onHoverChange?(hovering)
        }
        .contextMenu {
            Button("Quit Agent Dash HUD") { AppActions.quit() }
        }
        .fixedSize()
    }
}

/// Measures the physical notch on a screen. Falls back to a plausible housing
/// when the auxiliary-area API reports nothing (it never should in notch mode,
/// since ModePolicy already required a notch to get here).
struct NotchGeometry {
    let cameraWidth: CGFloat
    let faceHeight: CGFloat

    @MainActor
    init(screen: NSScreen) {
        let inset = screen.safeAreaInsets.top
        faceHeight = inset > 0 ? inset : 34
        if let left = screen.auxiliaryTopLeftArea, let right = screen.auxiliaryTopRightArea {
            cameraWidth = max(80, screen.frame.width - left.width - right.width)
        } else {
            cameraWidth = 180
        }
    }
}

extension NSScreen {
    /// True for the laptop's own panel.
    var isBuiltIn: Bool {
        guard let id = deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber
        else { return false }
        return CGDisplayIsBuiltin(CGDirectDisplayID(id.uint32Value)) != 0
    }

    /// The pure-policy description of this screen.
    @MainActor
    var displayInfo: DisplayInfo {
        let inset = safeAreaInsets.top
        var notchWidth: CGFloat = 0
        if inset > 0, let left = auxiliaryTopLeftArea, let right = auxiliaryTopRightArea {
            notchWidth = max(0, frame.width - left.width - right.width)
        }
        return DisplayInfo(isBuiltIn: isBuiltIn, notchHeight: inset, notchWidth: notchWidth)
    }
}
