import AppKit
import Combine
import SwiftUI
import HUDCore

/// Wires the HUD to one store as a plain menu-bar app. An NSStatusItem hosts the
/// glance (a brand-marked quota ring cluster per subscription), rendered as a
/// monochrome template image so macOS keeps it legible over any wallpaper.
/// Left-click opens the full card panel below it; right-click offers Quit
/// (there's no dock icon or app menu).
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let store = HUDStore()
    private var statusItem: NSStatusItem!
    private var panel: NSPanel?
    /// Redraws the glance whenever the store publishes a new snapshot.
    private var cancellables = Set<AnyCancellable>()
    /// The daemon we spawned, if we did, so we can stop it again on quit and not
    /// leave an orphan behind.
    private var daemonProcess: Process?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Launching the app is all it takes: bring up the data daemon ourselves
        // rather than making the user run `adash serve` in a second terminal.
        daemonProcess = DaemonLauncher.ensureRunning()
        store.start()

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)

        if let button = statusItem.button {
            button.target = self
            button.action = #selector(togglePanel)
            // Left-click toggles the card; right-click offers Quit.
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }

        // Re-render the template glance on every poll. Published fires the
        // current value on subscribe, so this also draws the initial state.
        renderGlance()
        store.$snapshot
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.renderGlance() }
            .store(in: &cancellables)
    }

    /// Rasterize the menu-bar glance to a template NSImage. Drawing it black and
    /// marking it a template hands the tint to AppKit, which colors it for the
    /// menu bar (white on a dark bar) and guarantees contrast over any wallpaper;
    /// only the alpha — mark solid, ring track faint, fill solid — carries through.
    private func renderGlance() {
        guard let button = statusItem?.button else { return }
        let glance = MenuBarContentView(snapshot: store.snapshot, now: store.now, tint: .black)
        let renderer = ImageRenderer(content: glance)
        renderer.scale = button.window?.backingScaleFactor ?? NSScreen.main?.backingScaleFactor ?? 2
        renderer.isOpaque = false
        guard let image = renderer.nsImage else { return }
        image.isTemplate = true
        button.image = image
        button.imagePosition = .imageOnly
    }

    func applicationWillTerminate(_ notification: Notification) {
        store.stop()
        // Only stop the daemon if we started it; leave a user-run one alone.
        daemonProcess?.terminate()
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

/// SwiftUI wrapper for the card so it redraws on every poll.
private struct CardHost: View {
    @EnvironmentObject var store: HUDStore
    var body: some View {
        PopoverCard(snapshot: store.snapshot, now: store.now)
            .padding(10)
    }
}
