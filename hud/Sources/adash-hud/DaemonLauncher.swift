import Foundation

/// Starts the Python snapshot daemon so launching the menu-bar app is the only
/// thing you do — no separate `adash serve` in another terminal. On launch the
/// HUD checks whether a daemon is already answering on the loopback port; if
/// not, it finds the repo's `main.py` + `.venv` by walking up from its own
/// executable and spawns `python main.py serve`. The spawned process is handed
/// back so the app can stop it again when it quits, leaving no orphan daemon.
enum DaemonLauncher {
    static let port = 8737

    /// Ensure a daemon is running; returns the process we spawned (nil if one
    /// was already up, or if we couldn't locate the daemon to start it).
    static func ensureRunning() -> Process? {
        if isReachable() { return nil }
        guard let paths = resolvePaths() else {
            let message = "adash-hud: couldn't find the daemon (main.py + .venv) near the app; "
                + "start it manually with `python main.py serve`\n"
            FileHandle.standardError.write(Data(message.utf8))
            return nil
        }
        let proc = Process()
        proc.executableURL = paths.python
        proc.arguments = [paths.mainPy.path, "serve"]
        proc.currentDirectoryURL = paths.mainPy.deletingLastPathComponent()
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
            return proc
        } catch {
            FileHandle.standardError.write(Data("adash-hud: failed to start daemon: \(error)\n".utf8))
            return nil
        }
    }

    /// A quick synchronous liveness check against /v1/health. Runs once at
    /// startup, so a short block is fine.
    static func isReachable() -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/v1/health") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 0.6
        let semaphore = DispatchSemaphore(value: 0)
        var ok = false
        let task = URLSession.shared.dataTask(with: request) { _, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode == 200 { ok = true }
            semaphore.signal()
        }
        task.resume()
        _ = semaphore.wait(timeout: .now() + 1.0)
        return ok
    }

    /// Walk up from the executable looking for a directory that has both
    /// `main.py` and `.venv/bin/python`. That's the repo/worktree root when the
    /// binary lives at `<root>/hud/.build/<config>/adash-hud`.
    static func resolvePaths() -> (python: URL, mainPy: URL)? {
        let exe = (Bundle.main.executableURL
            ?? URL(fileURLWithPath: CommandLine.arguments.first ?? "")).resolvingSymlinksInPath()
        var dir = exe.deletingLastPathComponent()
        for _ in 0..<6 {
            let mainPy = dir.appendingPathComponent("main.py")
            let python = dir.appendingPathComponent(".venv/bin/python")
            if FileManager.default.fileExists(atPath: mainPy.path),
               FileManager.default.fileExists(atPath: python.path) {
                return (python, mainPy)
            }
            dir = dir.deletingLastPathComponent()
        }
        return nil
    }
}
