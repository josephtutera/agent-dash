import AppKit
import SwiftUI
import HUDCore

/// Wires the two faces of the HUD to one store. In menubar mode an
/// NSStatusItem hosts the mini ring clusters and clicking it opens the card
/// panel. In notch mode (built-in display is the sole display and has a
/// notch) the status item hides and a NotchController draws the pill over the
/// camera housing instead, expanding to the same card on hover. The mode is
/// re-decided from ModePolicy on every display-configuration change, and the
/// notch window is rebuilt rather than moved.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let store = HUDStore()
    private var statusItem: NSStatusItem!
    private var panel: NSPanel?
    private var hostingView: NSHostingView<AnyView>!
    private var notchController: NotchController!

    func applicationDidFinishLaunching(_ notification: Notification) {
        store.start()
        notchController = NotchController(store: store)

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)

        // Wrap so the hosting view observes the store and resizes to content.
        let root = MenuBarHost().environmentObject(store)
        hostingView = NSHostingView(rootView: AnyView(root))
        hostingView.translatesAutoresizingMaskIntoConstraints = false

        if let button = statusItem.button {
            button.addSubview(hostingView)
            NSLayoutConstraint.activate([
                hostingView.leadingAnchor.constraint(equalTo: button.leadingAnchor),
                hostingView.trailingAnchor.constraint(equalTo: button.trailingAnchor),
                hostingView.topAnchor.constraint(equalTo: button.topAnchor),
                hostingView.bottomAnchor.constraint(equalTo: button.bottomAnchor),
            ])
            button.target = self
            button.action = #selector(togglePanel)
        }

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(displaysChanged),
            name: NSApplication.didChangeScreenParametersNotification,
            object: nil
        )
        applyMode()
    }

    func applicationWillTerminate(_ notification: Notification) {
        store.stop()
        notchController.hide()
    }

    // MARK: - Mode switching

    @objc private func displaysChanged() {
        applyMode()
    }

    private func applyMode() {
        let displays = NSScreen.screens.map(\.displayInfo)
        let mode = ModePolicy.mode(for: displays)
        if mode == .notch, let builtIn = NSScreen.screens.first(where: \.isBuiltIn) {
            statusItem.isVisible = false
            panel?.orderOut(nil)
            notchController.show(on: builtIn)
        } else {
            notchController.hide()
            statusItem.isVisible = true
        }
    }

    // MARK: - Menubar card panel

    @objc private func togglePanel() {
        if let panel, panel.isVisible {
            panel.orderOut(nil)
            return
        }
        showPanel()
    }

    private func showPanel() {
        let card = CardHost().environmentObject(store)
        let hosting = NSHostingController(rootView: AnyView(card))

        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 500, height: 600),
            styleMask: [.nonactivatingPanel, .borderless, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        panel.contentViewController = hosting
        panel.isFloatingPanel = true
        panel.level = .statusBar
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isOpaque = false
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        // Position just below the status item, right-aligned to it.
        if let button = statusItem.button, let screen = button.window?.screen {
            let buttonFrame = button.window?.convertToScreen(button.frame) ?? .zero
            let size = hosting.view.fittingSize
            var origin = NSPoint(
                x: buttonFrame.maxX - size.width,
                y: buttonFrame.minY - size.height - 6
            )
            origin.x = max(screen.visibleFrame.minX + 8, origin.x)
            panel.setContentSize(size)
            panel.setFrameOrigin(origin)
        }

        panel.orderFrontRegardless()
        self.panel = panel
    }
}

/// SwiftUI wrapper so the menubar content redraws when the store publishes.
private struct MenuBarHost: View {
    @EnvironmentObject var store: HUDStore
    var body: some View {
        MenuBarContentView(snapshot: store.snapshot, now: store.now)
    }
}

/// SwiftUI wrapper for the card so it redraws on every poll.
private struct CardHost: View {
    @EnvironmentObject var store: HUDStore
    var body: some View {
        PopoverCard(snapshot: store.snapshot, now: store.now)
            .padding(10)
    }
}
