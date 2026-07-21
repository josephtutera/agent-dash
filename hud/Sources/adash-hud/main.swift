import AppKit
import HUDCore

// Entry point. Two modes:
//   adash-hud                          -> run the menubar app
//   adash-hud --render-preview out.png -> render the card to a PNG and exit
// The preview mode is how reviewers (and CI) see the UI without a menubar.

let args = CommandLine.arguments

if let flagIndex = args.firstIndex(of: "--render-preview") {
    let outPath = args.indices.contains(flagIndex + 1) ? args[flagIndex + 1] : "preview.png"
    let url = URL(fileURLWithPath: outPath)
    // ImageRenderer needs the AppKit machinery initialized, but not a full run
    // loop. Touch the shared application, then render on the main actor.
    _ = NSApplication.shared
    NSApp.setActivationPolicy(.prohibited)
    do {
        _ = try MainActor.assumeIsolated {
            try PreviewRenderer.renderCardPNG(to: url, scale: 2)
        }
        FileHandle.standardError.write(Data("rendered preview to \(url.path)\n".utf8))
        exit(0)
    } catch {
        FileHandle.standardError.write(Data("preview render failed: \(error)\n".utf8))
        exit(1)
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory) // LSUIElement-style: no dock icon
    app.run()
}
