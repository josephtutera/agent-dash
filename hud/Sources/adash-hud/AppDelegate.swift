import AppKit
import SwiftUI
import HUDCore

/// Wires the HUD to one store as a plain menu-bar app. An NSStatusItem hosts the
/// glance (one colored dot per running agent, spinning while it works, plus a
/// single ring for the tightest quota). Left-click opens the full card panel
/// below it; right-click offers Quit (there's no dock icon or app menu).
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let store = HUDStore()
    private var statusItem: NSStatusItem!
    private var panel: NSPanel?
    private var hostingView: NSHostingView<AnyView>!

    func applicationDidFinishLaunching(_ notification: Notification) {
        store.start()

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
            // Left-click toggles the card; right-click offers Quit.
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        store.stop()
    }

    // MARK: - Status item interaction

    @objc private func togglePanel() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            showStatusMenu()
            return
        }
        if let panel, panel.isVisible {
            panel.orderOut(nil)
            return
        }
        showPanel()
    }

    private func showStatusMenu() {
        let menu = NSMenu()
        menu.addItem(
            withTitle: "Quit Agent Dash HUD",
            action: #selector(quitApp),
            keyEquivalent: "q"
        )
        if let button = statusItem.button {
            menu.popUp(
                positioning: nil,
                at: NSPoint(x: 0, y: button.bounds.height + 4),
                in: button
            )
        }
    }

    @objc private func quitApp() {
        AppActions.quit()
    }

    // MARK: - Card panel

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
