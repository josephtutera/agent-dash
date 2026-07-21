import AppKit
import HUDCore

// Entry point. Three modes:
//   adash-hud                                -> run the HUD (notch or menubar)
//   adash-hud --render-preview out.png       -> render the card to a PNG and exit
//   adash-hud --render-preview-notch out.png -> render the notch pill (collapsed
//                                               and hover-expanded) and exit
// The preview modes are how reviewers (and CI) see the UI without a menubar.

let args = CommandLine.arguments

func runRender(_ flag: String, defaultName: String, render: @MainActor (URL) throws -> Void) {
    guard let flagIndex = args.firstIndex(of: flag) else { return }
    let outPath = args.indices.contains(flagIndex + 1) ? args[flagIndex + 1] : defaultName
    let url = URL(fileURLWithPath: outPath)
    // ImageRenderer needs the AppKit machinery initialized, but not a full run
    // loop. Touch the shared application, then render on the main actor.
    _ = NSApplication.shared
    NSApp.setActivationPolicy(.prohibited)
    do {
        try MainActor.assumeIsolated {
            try render(url)
        }
        FileHandle.standardError.write(Data("rendered preview to \(url.path)\n".utf8))
        exit(0)
    } catch {
        FileHandle.standardError.write(Data("preview render failed: \(error)\n".utf8))
        exit(1)
    }
}

runRender("--render-preview", defaultName: "preview.png") { url in
    try PreviewRenderer.renderCardPNG(to: url, scale: 2)
}
runRender("--render-preview-notch", defaultName: "preview-notch.png") { url in
    try PreviewRenderer.renderNotchPNG(to: url, scale: 2)
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory) // LSUIElement-style: no dock icon
    app.run()
}
