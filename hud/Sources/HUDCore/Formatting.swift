import Foundation

// Pure formatting helpers. Everything here is deterministic given an explicit
// `now`, so the unit tests can pin the clock and the views stay dumb.
public enum Fmt {

    /// Consumed fraction of a limit, in 0...1, from percent remaining.
    /// A null reading is treated as nothing consumed (empty ring/bar).
    public static func consumed(pctLeft: Int?) -> Double {
        guard let pct = pctLeft else { return 0 }
        let clamped = min(100, max(0, pct))
        return Double(100 - clamped) / 100.0
    }

    /// Remaining fraction of a limit, in 0...1, from percent remaining. This is
    /// the fill for the menu-bar glance's micro fuel-bar, which reads as a fuel
    /// gauge (a short bar means little left), so it matches its own number
    /// rather than the popover meters, which fill by `consumed`. A null reading
    /// is treated as empty.
    public static func remaining(pctLeft: Int?) -> Double {
        guard let pct = pctLeft else { return 0 }
        return Double(min(100, max(0, pct))) / 100.0
    }

    /// The bare percent-remaining glyph the glance shows, e.g. "18" or "--".
    /// No "%": the fuel bar beneath already reads it as a proportion.
    public static func glancePercent(pctLeft: Int?) -> String {
        pctLeft.map(String.init) ?? "--"
    }

    /// Compact time-until-reset from now, per the design's countdown ladder:
    ///   < 1h   -> "37m"
    ///   < 1d   -> "2h06"   (hours, then zero-padded minutes)
    ///   < 7d   -> "6d22h"  (days, then hours)
    ///   >= 7d  -> "Jul 24" (a plain reset date; too far out to count down)
    /// A reset that is already in the past reads "0m".
    public static func countdown(to resetsAt: Date, now: Date = Date()) -> String {
        let seconds = resetsAt.timeIntervalSince(now)
        if seconds <= 0 { return "0m" }

        let totalMinutes = Int(seconds / 60)
        let totalHours = Int(seconds / 3600)
        let totalDays = Int(seconds / 86_400)

        if totalHours < 1 {
            return "\(max(1, totalMinutes))m"
        }
        if totalDays < 1 {
            let hours = totalHours
            let minutes = totalMinutes - hours * 60
            return "\(hours)h\(String(format: "%02d", minutes))"
        }
        if totalDays < 7 {
            let days = totalDays
            let hours = totalHours - days * 24
            return "\(days)d\(hours)h"
        }
        return dateLabel(resetsAt)
    }

    /// A far-out reset date rendered like "Jul 24" in the user's locale.
    public static func dateLabel(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "MMM d"
        return f.string(from: date)
    }

    /// Wall-clock time like "11:47a" / "9:41a" used in status and pace lines.
    public static func clock(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "h:mma"
        var s = f.string(from: date)
        s = s.replacingOccurrences(of: "AM", with: "a")
             .replacingOccurrences(of: "PM", with: "p")
        return s
    }

    /// The short glyph a meter row uses for a window kind.
    public static func windowLabel(kind: String) -> String {
        switch kind {
        case "session_5h":   return "5h"
        case "weekly_7d":    return "7d"
        case "weekly_fable": return "F"
        case "weekly":       return "7d"
        default:             return "•"
        }
    }

    /// The right-aligned meter value, e.g. "74% · 1h12".
    public static func meterValue(pctLeft: Int?, resetsAt: Date?, now: Date = Date()) -> String {
        let pctPart = pctLeft.map { "\($0)%" } ?? "--%"
        guard let reset = resetsAt else { return pctPart }
        return "\(pctPart) · \(countdown(to: reset, now: now))"
    }

    /// A US-dollar readout for the value tiles, e.g. "$182" or "$4.1k".
    public static func usd(_ amount: Double) -> String {
        if amount >= 10_000 {
            return String(format: "$%.1fk", amount / 1000)
        }
        if amount >= 1000 {
            return String(format: "$%.1fk", amount / 1000)
        }
        return "$" + String(format: "%.0f", amount)
    }

    /// The API-value multiple, e.g. "12.6×".
    public static func multiple(_ x: Double) -> String {
        String(format: "%.1f×", x)
    }

    /// Time-in-state for an agent row, e.g. "1m12" / "44s" / "2h06".
    public static func sinceLabel(seconds: Int?) -> String {
        guard let s = seconds, s >= 0 else { return "" }
        if s < 60 { return "\(s)s" }
        let minutes = s / 60
        if minutes < 60 {
            let secs = s - minutes * 60
            return "\(minutes)m\(String(format: "%02d", secs))"
        }
        let hours = s / 3600
        let mins = (s - hours * 3600) / 60
        return "\(hours)h\(String(format: "%02d", mins))"
    }
}
