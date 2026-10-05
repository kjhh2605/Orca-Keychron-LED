import AppKit
import Foundation
import SwiftUI

enum LEDEffect: String, Codable, CaseIterable, Identifiable {
    case solid, breathe, blink, double, triple, off
    var id: String { rawValue }
    var label: String {
        switch self {
        case .solid: "계속 켜짐"
        case .breathe: "숨쉬기"
        case .blink: "깜빡임"
        case .double: "두 번 깜빡임"
        case .triple: "세 번 깜빡임"
        case .off: "꺼짐"
        }
    }
    var animated: Bool { self != .solid && self != .off }

    // Matches the firmware's phase windows. Reduce Motion renders a steady sample.
    func amplitude(at seconds: TimeInterval, period: Int, reduceMotion: Bool) -> Double {
        guard self != .off else { return 0 }
        guard animated && !reduceMotion else { return 1 }
        let phase = seconds.truncatingRemainder(dividingBy: Double(period) / 1000) / (Double(period) / 1000)
        switch self {
        case .breathe: return (55 + 200 * (phase < 0.5 ? phase * 2 : (1 - phase) * 2)) / 255
        case .blink: return phase < 0.5 ? 1 : 0
        case .double: return phase < 0.1 || (phase >= 0.2 && phase < 0.3) ? 1 : 0
        case .triple: return phase < 0.1 || (phase >= 0.2 && phase < 0.3) || (phase >= 0.4 && phase < 0.5) ? 1 : 0
        case .solid: return 1
        case .off: return 0
        }
    }
}

struct LEDStateTheme: Codable, Equatable, Identifiable {
    let state: Int
    var color: String
    var brightness: Int
    var effect: LEDEffect
    var period_ms: Int
    var id: Int { state }
    var label: String { Self.labels[state] ?? "확인 불가" }
    var detail: String {
        switch state {
        case 1: "에이전트가 쉬고 있어요."
        case 2: "에이전트가 작업하고 있어요."
        case 3: "답변이나 승인을 기다려요."
        case 4: "작업이 끝났고 아직 확인하지 않았어요."
        case 7: "끝난 작업을 Orca에서 열어 봤어요."
        case 5: "작업에 문제가 생겼어요."
        default: "현재 상태를 확인할 수 없어요."
        }
    }
    var symbol: String {
        [1: "pause.fill", 2: "arrow.triangle.2.circlepath", 3: "questionmark.bubble.fill", 4: "checkmark", 7: "checkmark.circle", 5: "exclamationmark.triangle.fill", 6: "ellipsis"][state] ?? "ellipsis"
    }
    static let labels = [0: "미할당", 1: "대기", 2: "진행 중", 3: "질문·승인 대기", 4: "완료", 5: "오류", 6: "확인 불가", 7: "완료 · 확인함"]
    /// Acknowledged completion is listed next to completion.
    static let displayOrder = [1, 2, 3, 4, 7, 5, 6]
    static func canonicalHex(_ text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let digits = trimmed.hasPrefix("#") ? String(trimmed.dropFirst()) : trimmed
        guard digits.count == 6, digits.allSatisfy({ $0.isASCII && $0.isHexDigit }) else { return nil }
        return "#" + digits.uppercased()
    }
    var channels: (Double, Double, Double) {
        let value = UInt32(color.dropFirst(), radix: 16) ?? 0
        return (Double((value >> 16) & 255) / 255, Double((value >> 8) & 255) / 255, Double(value & 255) / 255)
    }
    var displayColor: Color {
        let (red, green, blue) = channels
        return Color(.sRGB, red: red, green: green, blue: blue, opacity: 1)
    }
    static func hex(from color: Color) -> String? {
        guard let rgb = NSColor(color).usingColorSpace(.sRGB) else { return nil }
        func channel(_ value: CGFloat) -> Int { Int((min(1, max(0, value)) * 255).rounded()) }
        return String(format: "#%02X%02X%02X", channel(rgb.redComponent), channel(rgb.greenComponent), channel(rgb.blueComponent))
    }
}

struct LEDTheme: Codable, Equatable {
    var schema: Int = 1
    var revision: String
    var global_brightness: Int
    var states: [LEDStateTheme]

    static var defaults: LEDTheme {
        LEDTheme(revision: UUID().uuidString.lowercased(), global_brightness: 72, states: [
            LEDStateTheme(state: 1, color: "#2A2A2A", brightness: 100, effect: .solid, period_ms: 2400),
            LEDStateTheme(state: 2, color: "#0033FF", brightness: 100, effect: .breathe, period_ms: 2400),
            LEDStateTheme(state: 3, color: "#FF5A00", brightness: 100, effect: .double, period_ms: 2400),
            LEDStateTheme(state: 4, color: "#0CFF00", brightness: 100, effect: .blink, period_ms: 2400),
            LEDStateTheme(state: 7, color: "#0CFF00", brightness: 100, effect: .solid, period_ms: 2400),
            LEDStateTheme(state: 5, color: "#FF0000", brightness: 100, effect: .triple, period_ms: 2800),
            LEDStateTheme(state: 6, color: "#9600DC", brightness: 100, effect: .blink, period_ms: 2400)
        ])
    }
    func validated() throws -> LEDTheme {
        var states = self.states
        // Themes saved before acknowledged completion keep the old completion look for it.
        if states.map(\.state).sorted() == Array(1...6), let complete = states.first(where: { $0.state == 4 }) {
            states.append(LEDStateTheme(state: 7, color: complete.color, brightness: complete.brightness, effect: .solid, period_ms: complete.period_ms))
        }
        guard schema == 1, UUID(uuidString: revision) != nil, (0...255).contains(global_brightness),
              states.map(\.state).sorted() == Array(1...7) else {
            throw ThemeError.invalid("설정 파일 형식이 올바르지 않습니다.")
        }
        for state in states {
            guard state.color.count == 7, state.color.hasPrefix("#"), LEDStateTheme.canonicalHex(state.color) != nil, (0...100).contains(state.brightness),
                  (800...10000).contains(state.period_ms), state.period_ms % 100 == 0 else {
                throw ThemeError.invalid("색상·밝기·주기 값을 확인해 주세요.")
            }
        }
        var result = self
        result.states = states
        for index in result.states.indices { result.states[index].color = result.states[index].color.uppercased() }
        let order = LEDStateTheme.displayOrder
        result.states.sort { order.firstIndex(of: $0.state)! < order.firstIndex(of: $1.state)! }
        return result
    }
    func sameSettings(as other: LEDTheme) -> Bool {
        global_brightness == other.global_brightness && states == other.states && schema == other.schema
    }
    func encoded() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(validated())
    }
    func state(_ id: Int) -> LEDStateTheme? { states.first { $0.state == id } }

    static func decode(_ data: Data) throws -> LEDTheme {
        guard data.count <= 65_536 else { throw ThemeError.invalid("설정 파일은 64KB 이하여야 합니다.") }
        try rejectDuplicateJSONKeys(data)
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(root.keys) == ["schema", "revision", "global_brightness", "states"],
              isJSONInteger(root["schema"]), isJSONInteger(root["global_brightness"]),
              let rows = root["states"] as? [[String: Any]],
              rows.allSatisfy({ Set($0.keys) == ["state", "color", "brightness", "effect", "period_ms"]
                  && isJSONInteger($0["state"]) && isJSONInteger($0["brightness"]) && isJSONInteger($0["period_ms"]) }) else {
            throw ThemeError.invalid("알 수 없거나 누락된 설정 항목이 있습니다.")
        }
        return try JSONDecoder().decode(LEDTheme.self, from: data).validated()
    }
}

enum ThemeError: LocalizedError {
    case invalid(String)
    var errorDescription: String? {
        switch self { case .invalid(let message): message }
    }
}
