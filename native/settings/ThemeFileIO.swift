import Foundation

enum ThemeFileRead: Sendable {
    case data(Data)
    case missing
    case failed(String, denied: Bool)
    var data: Data? { if case .data(let value) = self { value } else { nil } }
    var readable: Bool { if case .failed = self { false } else { true } }
    var denied: Bool { if case .failed(_, let denied) = self { denied } else { false } }
}

struct ThemeFilesSnapshot: Sendable {
    let theme: ThemeFileRead
    let firmware: ThemeFileRead
    let status: ThemeFileRead
    var denied: Bool { theme.denied || firmware.denied || status.denied }
}

// A dedicated serial queue keeps TCC's synchronous permission wait completely
// outside the main actor and limits the app to one outstanding filesystem job.
final class ThemeFileIO: @unchecked Sendable {
    private let queue = DispatchQueue(label: "local.orca-keychron-led.settings.files", qos: .utility)
    private let runtime: URL
    private let testDelay: TimeInterval
    init(runtime: URL, testDelay: TimeInterval = 0) { self.runtime = runtime; self.testDelay = testDelay }

    func read() async -> ThemeFilesSnapshot {
        await withCheckedContinuation { continuation in
            queue.async { [self] in
                if testDelay > 0 { Thread.sleep(forTimeInterval: testDelay) }
                continuation.resume(returning: ThemeFilesSnapshot(
                    theme: Self.readFile(runtime.appendingPathComponent("theme.json"), limit: 65_536),
                    firmware: Self.readFile(runtime.appendingPathComponent("theme-firmware.json")),
                    status: Self.readFile(runtime.appendingPathComponent("status.json"))))
            }
        }
    }

    func save(_ data: Data, expected: Data?, preserveInvalid: Bool) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            queue.async { [self] in
                do {
                    if testDelay > 0 { Thread.sleep(forTimeInterval: testDelay) }
                    let source = runtime.appendingPathComponent("theme.json")
                    let current = Self.readFile(source, limit: 65_536)
                    guard current.readable else { throw ThemeError.invalid("설정 폴더에 접근할 수 없습니다. 파일 및 폴더 권한을 확인해 주세요.") }
                    guard current.data == expected else { throw ThemeError.invalid("다른 곳에서 설정이 변경되었습니다. 다시 불러온 뒤 저장해 주세요.") }
                    try FileManager.default.createDirectory(at: runtime, withIntermediateDirectories: true)
                    if preserveInvalid, let original = current.data {
                        let backup = runtime.appendingPathComponent("theme.recovery-\(UUID().uuidString.lowercased()).json")
                        try original.write(to: backup, options: .atomic)
                    }
                    try data.write(to: source, options: .atomic)
                    continuation.resume()
                } catch { continuation.resume(throwing: error) }
            }
        }
    }

    private static func readFile(_ url: URL, limit: Int = 1_048_576) -> ThemeFileRead {
        do {
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            let data = try handle.read(upToCount: limit + 1) ?? Data()
            guard data.count <= limit else { return .failed("설정 파일이 너무 큽니다.", denied: false) }
            return .data(data)
        } catch {
            let value = error as NSError
            if value.domain == NSCocoaErrorDomain && [NSFileReadNoSuchFileError, NSFileNoSuchFileError].contains(value.code)
                || value.domain == NSPOSIXErrorDomain && value.code == 2 { return .missing }
            let denied = value.domain == NSCocoaErrorDomain && value.code == NSFileReadNoPermissionError
                || value.domain == NSPOSIXErrorDomain && [1, 13].contains(value.code)
            return .failed(value.localizedDescription, denied: denied)
        }
    }
}
