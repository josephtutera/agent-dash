import SwiftUI

/// Owns the live snapshot and the polling loop. Fetches the daemon every 2s
/// over HTTP with a tight timeout; on failure it falls back to the on-disk
/// cache the daemon also writes; if both fail the snapshot goes nil and the UI
/// shows its offline state. A separate 30s ticker nudges `now` so countdowns
/// keep moving without refetching.
@MainActor
public final class HUDStore: ObservableObject {
    @Published public private(set) var snapshot: HUDSnapshot?
    @Published public private(set) var now: Date = Date()
    @Published public private(set) var isOffline: Bool = true

    private let endpoint = URL(string: "http://127.0.0.1:8737/v1/hud")!
    private let cacheURL: URL = FileManager.default
        .homeDirectoryForCurrentUser
        .appendingPathComponent(".cache/adash/hud.json")

    private let session: URLSession
    private var pollTask: Task<Void, Never>?
    private var tickTask: Task<Void, Never>?

    public init() {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 1.5
        config.timeoutIntervalForResource = 1.5
        config.waitsForConnectivity = false
        self.session = URLSession(configuration: config)
    }

    public func start() {
        stop()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
        tickTask = Task { [weak self] in
            while !Task.isCancelled {
                await MainActor.run { self?.now = Date() }
                try? await Task.sleep(nanoseconds: 30_000_000_000)
            }
        }
    }

    public func stop() {
        pollTask?.cancel(); pollTask = nil
        tickTask?.cancel(); tickTask = nil
    }

    public func refresh() async {
        if let fresh = await fetchFromDaemon() {
            apply(fresh)
            return
        }
        if let cached = readFromCache() {
            apply(cached)
            return
        }
        snapshot = nil
        isOffline = true
    }

    private func apply(_ snap: HUDSnapshot) {
        snapshot = snap
        now = Date()
        isOffline = false
    }

    private func fetchFromDaemon() async -> HUDSnapshot? {
        do {
            let (data, response) = try await session.data(from: endpoint)
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                return nil
            }
            return try HUDSnapshot.decode(from: data)
        } catch {
            return nil
        }
    }

    private func readFromCache() -> HUDSnapshot? {
        guard let data = try? Data(contentsOf: cacheURL) else { return nil }
        return try? HUDSnapshot.decode(from: data)
    }
}
