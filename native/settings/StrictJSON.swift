import Foundation
import CoreFoundation

func isJSONInteger(_ value: Any?) -> Bool {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return false }
    return !["f", "d"].contains(String(cString: number.objCType))
}

// Foundation's decoder validates JSON syntax but accepts duplicate object keys.
// This small preflight preserves parity with the bridge's strict JSON reader.
func rejectDuplicateJSONKeys(_ data: Data) throws {
    let bytes = Array(data)
    var objects: [Set<String>] = []
    var index = 0
    while index < bytes.count {
        switch bytes[index] {
        case 123: objects.append([]); index += 1 // {
        case 125:
            guard !objects.isEmpty else { throw ThemeError.invalid("설정 JSON이 올바르지 않습니다.") }
            objects.removeLast(); index += 1
        case 34:
            let start = index
            index += 1
            while index < bytes.count {
                if bytes[index] == 92 { index += 2; continue }
                if bytes[index] == 34 { break }
                index += 1
            }
            guard index < bytes.count else { throw ThemeError.invalid("설정 JSON 문자열이 올바르지 않습니다.") }
            index += 1
            var next = index
            while next < bytes.count && [9, 10, 13, 32].contains(bytes[next]) { next += 1 }
            if next < bytes.count && bytes[next] == 58 && !objects.isEmpty {
                let key = try JSONDecoder().decode(String.self, from: Data(bytes[start..<index]))
                guard objects[objects.count - 1].insert(key).inserted else {
                    throw ThemeError.invalid("설정에 중복된 항목이 있습니다: \(key)")
                }
            }
        default: index += 1
        }
    }
}
