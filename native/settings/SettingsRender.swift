import AppKit
import SwiftUI

@MainActor enum SettingsRender {
    static func writeDemo(to url: URL) throws {
        let store = ThemeStore(project: FileManager.default.temporaryDirectory, demoOnly: true)
        let view = SettingsView(store: store)
            .environment(\.renderStaticPreview, true)
            .environment(\.colorScheme, .dark)
            .frame(width: 1040, height: 758)
        // SwiftUI ImageRenderer cannot draw native macOS controls. Cache our own
        // offscreen hosting view instead; no screen/window capture is involved.
        let hosting = NSHostingView(rootView: view)
        let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 1040, height: 758),
                              styleMask: .borderless, backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentView = hosting
        hosting.frame = NSRect(x: 0, y: 0, width: 1040, height: 758)
        hosting.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
        RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        hosting.layoutSubtreeIfNeeded()
        guard let bitmap = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) else {
            throw ThemeError.invalid("설정 화면 이미지를 만들지 못했습니다.")
        }
        hosting.cacheDisplay(in: hosting.bounds, to: bitmap)
        guard let data = bitmap.representation(using: .png, properties: [:]) else { throw ThemeError.invalid("PNG를 만들지 못했습니다.") }
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try publicPNG(data).write(to: url, options: .atomic)
        print(url.path)
    }

    // Keep rendering/color chunks byte-for-byte, excluding private metadata and
    // newer color tags that may conflict with the retained ICC profile.
    static func publicPNG(_ data: Data) throws -> Data {
        let bytes = [UInt8](data)
        let signature: [UInt8] = [137, 80, 78, 71, 13, 10, 26, 10]
        let allowed: Set<String> = ["IHDR", "IDAT", "IEND", "sRGB", "gAMA", "cHRM", "iCCP", "pHYs", "PLTE", "tRNS"]
        guard bytes.count >= 8, Array(bytes.prefix(8)) == signature else {
            throw ThemeError.invalid("PNG 서명이 올바르지 않습니다.")
        }
        var result = Data(signature)
        var offset = 8
        var sawHeader = false
        var sawImage = false
        var sawEnd = false
        func uint32(at index: Int) -> UInt32 {
            bytes[index..<index + 4].reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        }
        while offset < bytes.count {
            guard bytes.count - offset >= 12 else { throw ThemeError.invalid("PNG 청크가 잘렸습니다.") }
            let length = Int(uint32(at: offset))
            guard length <= bytes.count - offset - 12 else { throw ThemeError.invalid("PNG 청크 길이가 올바르지 않습니다.") }
            let typeBytes = Array(bytes[offset + 4..<offset + 8])
            guard typeBytes.allSatisfy({ (65...90).contains($0) || (97...122).contains($0) }),
                  typeBytes[2] & 32 == 0,
                  let type = String(bytes: typeBytes, encoding: .ascii) else {
                throw ThemeError.invalid("PNG 청크 종류가 올바르지 않습니다.")
            }
            let end = offset + length + 12
            guard crc32(bytes[(offset + 4)..<(end - 4)]) == uint32(at: end - 4) else {
                throw ThemeError.invalid("PNG 청크 무결성 검사에 실패했습니다.")
            }
            if type == "IHDR" {
                guard !sawHeader, offset == 8, length == 13 else { throw ThemeError.invalid("PNG 헤더가 올바르지 않습니다.") }
                sawHeader = true
            } else if !sawHeader {
                throw ThemeError.invalid("PNG 헤더가 없습니다.")
            }
            if type == "IDAT" { sawImage = true }
            if type == "IEND" {
                guard length == 0, sawImage, end == bytes.count else { throw ThemeError.invalid("PNG 끝부분이 올바르지 않습니다.") }
                sawEnd = true
            }
            if allowed.contains(type) {
                result.append(contentsOf: bytes[offset..<end])
            } else if typeBytes[0] & 32 == 0 {
                throw ThemeError.invalid("지원하지 않는 필수 PNG 청크입니다.")
            }
            offset = end
        }
        guard sawHeader, sawImage, sawEnd else { throw ThemeError.invalid("PNG가 완전하지 않습니다.") }
        return result
    }

    private static func crc32(_ bytes: ArraySlice<UInt8>) -> UInt32 {
        var crc: UInt32 = 0xffffffff
        for byte in bytes {
            crc ^= UInt32(byte)
            for _ in 0..<8 { crc = (crc >> 1) ^ (crc & 1 == 1 ? 0xedb88320 : 0) }
        }
        return crc ^ 0xffffffff
    }

    static func metadataFilterSelfTest() throws -> Int {
        guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1, pixelsHigh: 1,
                                           bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                           isPlanar: false, colorSpaceName: .deviceRGB,
                                           bytesPerRow: 4, bitsPerPixel: 32) else {
            throw ThemeError.invalid("PNG 시험 이미지를 만들지 못했습니다.")
        }
        bitmap.setColor(.black, atX: 0, y: 0)
        guard let raw = bitmap.representation(using: .png, properties: [:]) else {
            throw ThemeError.invalid("PNG 시험 이미지를 인코딩하지 못했습니다.")
        }
        let clean = try publicPNG(raw)
        var count = 0
        func check(_ condition: Bool, _ name: String) throws {
            guard condition else { throw ThemeError.invalid(name) }
            count += 1
        }
        func rejected(_ input: Data, _ name: String) throws {
            do { _ = try publicPNG(input) } catch { count += 1; return }
            throw ThemeError.invalid(name)
        }
        func chunk(_ name: String, _ payload: [UInt8]) -> Data {
            let body = Array(name.utf8) + payload
            var result = Data()
            for value in [UInt32(payload.count)] {
                result.append(contentsOf: [UInt8((value >> 24) & 255), UInt8((value >> 16) & 255), UInt8((value >> 8) & 255), UInt8(value & 255)])
            }
            result.append(contentsOf: body)
            let crc = crc32(body[...])
            result.append(contentsOf: [UInt8((crc >> 24) & 255), UInt8((crc >> 16) & 255), UInt8((crc >> 8) & 255), UInt8(crc & 255)])
            return result
        }
        var decorated = Data(clean.dropLast(12))
        for name in ["eXIf", "cICP", "tEXt", "iTXt", "zTXt"] { decorated.append(chunk(name, [1, 2, 3])) }
        decorated.append(clean.suffix(12))
        try check(try publicPNG(decorated) == clean, "PNG metadata removed without changing retained chunks")
        try check(try publicPNG(clean) == clean, "PNG filtering is idempotent")
        try check(NSBitmapImageRep(data: clean) != nil, "Filtered PNG decodes")
        var bad = clean; bad[0] = 0
        try rejected(bad, "PNG rejects invalid signature")
        try rejected(Data(clean.dropLast()), "PNG rejects truncation")
        bad = clean; bad[29] ^= 1
        try rejected(bad, "PNG rejects CRC mismatch")
        bad = Data(clean.dropLast(12)); bad.append(chunk("ABCD", [])); bad.append(clean.suffix(12))
        try rejected(bad, "PNG rejects unknown critical chunk")
        bad = clean; bad.append(0)
        try rejected(bad, "PNG rejects trailing bytes")
        bad = clean; bad.replaceSubrange(8..<12, with: [255, 255, 255, 255])
        try rejected(bad, "PNG rejects overflowing chunk length")
        return count
    }
}
