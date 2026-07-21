import Foundation

// Codable structs mirroring the daemon contract served at
// http://127.0.0.1:8737/v1/hud (version 1). Field names match the JSON
// exactly; do not rename them. Anything the daemon may omit or send as null
// is modeled as an Optional so decoding never throws on a sparse snapshot.

public struct HUDSnapshot: Codable, Equatable {
    public let version: Int
    public let generatedAt: Date?
    public let subscriptions: [Subscription]
    public let agents: [Agent]
    public let value: ValueBlock?
    public let soonestReset: SoonestReset?

    enum CodingKeys: String, CodingKey {
        case version
        case generatedAt = "generated_at"
        case subscriptions
        case agents
        case value
        case soonestReset = "soonest_reset"
    }

    public init(
        version: Int,
        generatedAt: Date?,
        subscriptions: [Subscription],
        agents: [Agent],
        value: ValueBlock?,
        soonestReset: SoonestReset?
    ) {
        self.version = version
        self.generatedAt = generatedAt
        self.subscriptions = subscriptions
        self.agents = agents
        self.value = value
        self.soonestReset = soonestReset
    }
}

public struct Subscription: Codable, Equatable, Identifiable {
    public let id: String            // "claude-team" | "claude-personal" | "codex"
    public let provider: String      // "claude" | "codex"
    public let label: String
    public let windows: [Window]
    public let tightest: Window?
    public let stale: String?        // null, or a reason string
    public let activeAgents: Int

    enum CodingKeys: String, CodingKey {
        case id, provider, label, windows, tightest, stale
        case activeAgents = "active_agents"
    }

    public init(
        id: String,
        provider: String,
        label: String,
        windows: [Window],
        tightest: Window?,
        stale: String?,
        activeAgents: Int
    ) {
        self.id = id
        self.provider = provider
        self.label = label
        self.windows = windows
        self.tightest = tightest
        self.stale = stale
        self.activeAgents = activeAgents
    }
}

public struct Window: Codable, Equatable {
    public let kind: String          // session_5h | weekly_7d | weekly_fable | weekly
    public let pctLeft: Int?
    public let resetsAt: Date?
    public let pace: Pace?

    enum CodingKeys: String, CodingKey {
        case kind
        case pctLeft = "pct_left"
        case resetsAt = "resets_at"
        case pace
    }

    public init(kind: String, pctLeft: Int?, resetsAt: Date?, pace: Pace?) {
        self.kind = kind
        self.pctLeft = pctLeft
        self.resetsAt = resetsAt
        self.pace = pace
    }
}

public struct Pace: Codable, Equatable {
    public let projectedDryAt: Date
    public let marginSeconds: Int

    enum CodingKeys: String, CodingKey {
        case projectedDryAt = "projected_dry_at"
        case marginSeconds = "margin_seconds"
    }

    public init(projectedDryAt: Date, marginSeconds: Int) {
        self.projectedDryAt = projectedDryAt
        self.marginSeconds = marginSeconds
    }
}

public struct Agent: Codable, Equatable, Identifiable {
    public let pid: Int
    public let tool: String          // "claude" | "codex" | "opencode"
    public let project: String
    public let cwd: String
    public let state: String         // "working" | "waiting" | "idle"
    public let action: String?
    public let sinceSeconds: Int?
    public let subscriptionID: String?

    public var id: Int { pid }

    enum CodingKeys: String, CodingKey {
        case pid, tool, project, cwd, state, action
        case sinceSeconds = "since_seconds"
        case subscriptionID = "subscription_id"
    }

    public init(
        pid: Int,
        tool: String,
        project: String,
        cwd: String,
        state: String,
        action: String?,
        sinceSeconds: Int?,
        subscriptionID: String?
    ) {
        self.pid = pid
        self.tool = tool
        self.project = project
        self.cwd = cwd
        self.state = state
        self.action = action
        self.sinceSeconds = sinceSeconds
        self.subscriptionID = subscriptionID
    }
}

public struct ValueBlock: Codable, Equatable {
    public let todayUSD: Double
    public let monthUSD: Double
    public let subsCostUSD: Double?
    public let multiple: Double?
    public let bySub: [String: SubValue]

    enum CodingKeys: String, CodingKey {
        case todayUSD = "today_usd"
        case monthUSD = "month_usd"
        case subsCostUSD = "subs_cost_usd"
        case multiple
        case bySub = "by_sub"
    }

    public init(
        todayUSD: Double,
        monthUSD: Double,
        subsCostUSD: Double?,
        multiple: Double?,
        bySub: [String: SubValue]
    ) {
        self.todayUSD = todayUSD
        self.monthUSD = monthUSD
        self.subsCostUSD = subsCostUSD
        self.multiple = multiple
        self.bySub = bySub
    }
}

public struct SubValue: Codable, Equatable {
    public let todayUSD: Double
    public let monthUSD: Double

    enum CodingKeys: String, CodingKey {
        case todayUSD = "today_usd"
        case monthUSD = "month_usd"
    }

    public init(todayUSD: Double, monthUSD: Double) {
        self.todayUSD = todayUSD
        self.monthUSD = monthUSD
    }
}

public struct SoonestReset: Codable, Equatable {
    public let subscriptionID: String
    public let kind: String
    public let resetsAt: Date

    enum CodingKeys: String, CodingKey {
        case subscriptionID = "subscription_id"
        case kind
        case resetsAt = "resets_at"
    }

    public init(subscriptionID: String, kind: String, resetsAt: Date) {
        self.subscriptionID = subscriptionID
        self.kind = kind
        self.resetsAt = resetsAt
    }
}

// MARK: - Decoding

extension HUDSnapshot {
    /// A JSONDecoder configured for the daemon contract's ISO8601 timestamps.
    /// The daemon may or may not emit fractional seconds, so we try both.
    public static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { d in
            let container = try d.singleValueContainer()
            let raw = try container.decode(String.self)
            if let date = ISO8601DateFormatter.hudWithFraction.date(from: raw)
                ?? ISO8601DateFormatter.hudPlain.date(from: raw) {
                return date
            }
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unparseable ISO8601 date: \(raw)"
            )
        }
        return decoder
    }

    public static func decode(from data: Data) throws -> HUDSnapshot {
        try makeDecoder().decode(HUDSnapshot.self, from: data)
    }
}

extension ISO8601DateFormatter {
    static let hudWithFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    static let hudPlain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
}
