import Foundation
import AppKit
import ApplicationServices
import IOKit
import IOKit.hid

// JSONL is the only stdout output. All UI and HID operations run on the main thread.
struct HelperFailure: Error {
    let code: String
    let message: String
}

func failure(_ code: String, _ message: String) -> HelperFailure {
    HelperFailure(code: code, message: message)
}

func emit(_ object: [String: Any]) {
    do {
        var data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        data.append(0x0A)
        FileHandle.standardOutput.write(data)
    } catch {
        FileHandle.standardError.write(Data("JSON output error\n".utf8))
    }
}

func integer(_ request: [String: Any], _ key: String, default defaultValue: Int? = nil) throws -> Int {
    guard let raw = request[key] else {
        if let value = defaultValue { return value }
        throw failure("invalid_argument", "Missing integer field: \(key)")
    }
    guard let number = raw as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue.isFinite,
          number.doubleValue == Double(number.intValue) else {
        throw failure("invalid_argument", "\(key) must be an integer")
    }
    return number.intValue
}

func bool(_ request: [String: Any], _ key: String, default defaultValue: Bool) throws -> Bool {
    guard let value = request[key] else { return defaultValue }
    guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else {
        throw failure("invalid_argument", "\(key) must be true or false")
    }
    return number.boolValue
}

func ioDescription(_ status: IOReturn) -> String {
    String(format: "IOKit error 0x%08x", UInt32(bitPattern: status))
}

func property(_ device: IOHIDDevice, _ key: String) -> Any? {
    IOHIDDeviceGetProperty(device, key as CFString)
}

func productID(_ device: IOHIDDevice) -> Int {
    (property(device, kIOHIDProductIDKey) as? NSNumber)?.intValue ?? -1
}

func transport(_ device: IOHIDDevice) -> String {
    (property(device, kIOHIDTransportKey) as? String) ?? "unknown"
}

func registryID(_ device: IOHIDDevice) -> UInt64 {
    var value: UInt64 = 0
    IORegistryEntryGetRegistryEntryID(IOHIDDeviceGetService(device), &value)
    return value
}

func allElements(_ device: IOHIDDevice) -> [IOHIDElement] {
    (IOHIDDeviceCopyMatchingElements(device, nil, IOOptionBits(kIOHIDOptionsTypeNone)) as? [IOHIDElement]) ?? []
}

func ledElements(_ device: IOHIDDevice) -> [Int: IOHIDElement] {
    var result: [Int: IOHIDElement] = [:]
    for element in allElements(device) where IOHIDElementGetUsagePage(element) == 0x08 && IOHIDElementGetType(element) == kIOHIDElementTypeOutput {
        result[Int(IOHIDElementGetUsage(element))] = element
    }
    return result
}

func describeLEDElement(_ element: IOHIDElement) -> [String: Any] {
    ["usage": Int(IOHIDElementGetUsage(element)), "report_id": Int(IOHIDElementGetReportID(element)),
     "report_size": Int(IOHIDElementGetReportSize(element)), "report_count": Int(IOHIDElementGetReportCount(element)),
     "logical_min": IOHIDElementGetLogicalMin(element), "logical_max": IOHIDElementGetLogicalMax(element),
     "cookie": Int(IOHIDElementGetCookie(element))]
}

func axValue(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var result: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &result) == .success else { return nil }
    return result
}

func axElement(_ element: AXUIElement, _ name: String) -> AXUIElement? {
    guard let value = axValue(element, name), CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return (value as! AXUIElement)
}

struct ConsumerSlotTracker {
    var held = Set<Int>()

    mutating func update(_ current: Set<Int>, assignedMask: Int) -> [Int] {
        let pressed = current.subtracting(held)
        held = current
        return pressed.sorted().filter { (0..<12).contains($0) && (assignedMask & (1 << $0)) != 0 }
    }
}

final class USBQuery {
    let registry: UInt64
    let report: UnsafeMutablePointer<UInt8>
    let request: [UInt8]
    let kind: String
    var response: [String: Any]?
    var error: HelperFailure?

    init(registry: UInt64, bytes: [UInt8], kind: String) {
        self.registry = registry
        self.request = bytes
        self.kind = kind
        report = .allocate(capacity: bytes.count)
        report.initialize(from: bytes, count: bytes.count)
    }

    deinit { report.deallocate() }
}

final class Helper {
    var device: IOHIDDevice?
    var leds: [Int: IOHIDElement] = [:]
    var removed = false
    var reportBuffer: UnsafeMutablePointer<UInt8>?
    var reportBufferSize = 0
    var listening = false
    var assignedMask = 0
    var slotTracker = ConsumerSlotTracker()
    var consumerReportsReceived = 0
    var rememberedWindows: [pid_t: AXUIElement] = [:]
    var signalSources: [DispatchSourceSignal] = []
    var stopping = false
    var pendingQuery: USBQuery?

    func discover() throws -> [IOHIDDevice] {
        // A fresh, unopened manager keeps discovery read-only and avoids stale
        // membership when a Bluetooth device reconnects between requests.
        let manager = IOHIDManagerCreate(kCFAllocatorDefault, IOOptionBits(kIOHIDOptionsTypeNone))
        IOHIDManagerSetDeviceMatching(manager, [kIOHIDVendorIDKey: 0x3434] as CFDictionary)
        let devices = (IOHIDManagerCopyDevices(manager) as? Set<IOHIDDevice>) ?? []
        return devices.filter {
            (property($0, kIOHIDVendorIDKey) as? NSNumber)?.intValue == 0x3434 && (0x02A0...0x02A5).contains(productID($0))
        }.sorted { registryID($0) < registryID($1) }
    }

    func describe(_ target: IOHIDDevice) -> [String: Any] {
        let elements = ledElements(target)
        return [
            "vendor_id": 0x3434,
            "product_id": productID(target),
            "product": property(target, kIOHIDProductKey) as? String ?? "Keychron K10 Pro",
            "transport": transport(target),
            "registry_id": String(registryID(target)),
            "usage_page": (property(target, kIOHIDPrimaryUsagePageKey) as? NSNumber)?.intValue ?? 0,
            "usage": (property(target, kIOHIDPrimaryUsageKey) as? NSNumber)?.intValue ?? 0,
            "led_usages": elements.keys.sorted(),
            "led_elements": elements.keys.sorted().compactMap { elements[$0].map(describeLEDElement) },
            "symbol_supported": elements.isEmpty ? NSNull() as Any : [3, 4, 5].allSatisfy { elements[$0] != nil },
            "element_access": elements.isEmpty ? "unavailable_until_device_open_or_permission_grant" : "available",
        ]
    }

    func permissions() -> [String: Any] {
        ["accessibility": AXIsProcessTrusted(), "input_monitoring": CGPreflightListenEventAccess()]
    }

    func requireAccessibility() throws {
        guard AXIsProcessTrusted() else { throw failure("accessibility_permission_required", "Accessibility access is required; permissions does not request it.") }
    }

    static func sessionDecision(onConsole: Bool?, loginDone: Bool?, sessionLocked: Bool?, consoleLocked: Bool?) -> [String: Any] {
        // Any affirmative lock signal wins. A missing CG lock key is common
        // when unlocked; require an explicit false signal from either source.
        let locked: Bool? = (sessionLocked == true || consoleLocked == true) ? true :
            ((sessionLocked == false || consoleLocked == false) ? false : nil)
        let known = onConsole != nil && loginDone != nil && locked != nil
        let active = known && onConsole == true && loginDone == true && locked == false
        return ["active": active, "reason": active ? "active_local_session" : (known ? "locked_or_inactive_session" : "session_state_unknown"),
                "on_console": onConsole as Any? ?? NSNull(), "login_done": loginDone as Any? ?? NSNull(),
                "screen_locked": locked as Any? ?? NSNull(),
                "cg_screen_locked": sessionLocked as Any? ?? NSNull(), "io_console_locked": consoleLocked as Any? ?? NSNull()]
    }

    func sessionStatus() -> [String: Any] {
        guard let session = CGSessionCopyCurrentDictionary() as? [String: Any] else {
            return ["active": false, "reason": "gui_session_unavailable", "on_console": NSNull(), "login_done": NSNull(), "screen_locked": NSNull()]
        }
        let onConsole = session[kCGSessionOnConsoleKey as String] as? Bool
        let loginDone = session[kCGSessionLoginDoneKey as String] as? Bool
        let locked = session["CGSSessionScreenIsLocked"] as? Bool
        var consoleLocked: Bool?
        let root = IORegistryGetRootEntry(kIOMainPortDefault)
        if root != 0 {
            defer { IOObjectRelease(root) }
            if let value = IORegistryEntryCreateCFProperty(root, "IOConsoleLocked" as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue(),
               CFGetTypeID(value) == CFBooleanGetTypeID() {
                consoleLocked = value as? Bool
            }
        }
        return Self.sessionDecision(onConsole: onConsole, loginDone: loginDone, sessionLocked: locked, consoleLocked: consoleLocked)
    }

    func requireActiveSession() throws {
        let state = sessionStatus()
        guard state["active"] as? Bool == true else {
            throw failure("session_inactive_or_unknown", "Navigation requires a confirmed unlocked local console session (\(state["reason"] ?? "unknown"))")
        }
    }

    func open(_ request: [String: Any]) throws -> [String: Any] {
        let product = try integer(request, "product_id", default: 0x02A0)
        guard (0x02A0...0x02A5).contains(product) else { throw failure("device_not_allowed", "Only Keychron K10 Pro product IDs 0x02A0...0x02A5 are allowed") }
        let connection = (request["transport"] as? String) ?? "Bluetooth"
        guard ["Bluetooth", "USB"].contains(connection) else { throw failure("invalid_argument", "transport must be Bluetooth or USB") }
        let wantedRegistry = request["registry_id"] as? String
        let matches = try discover().filter {
            productID($0) == product && transport($0).caseInsensitiveCompare(connection) == .orderedSame &&
            (wantedRegistry == nil || String(registryID($0)) == wantedRegistry)
        }
        let symbolMatches = matches.filter { candidate in [3, 4, 5].allSatisfy { ledElements(candidate)[$0] != nil } }
        let candidates = symbolMatches.isEmpty ? matches : symbolMatches
        guard !candidates.isEmpty else { throw failure("device_not_found", "Requested K10 Pro connection was not found") }
        guard candidates.count == 1 else { throw failure("ambiguous_device", "Specify registry_id from devices to select one HID interface") }
        if let current = device, registryID(current) == registryID(candidates[0]), !removed { return describe(current) }
        closeDevice()
        let selected = candidates[0]
        let status = IOHIDDeviceOpen(selected, IOOptionBits(kIOHIDOptionsTypeNone))
        guard status == kIOReturnSuccess else { throw failure("device_open_failed", "Could not open without seizing: \(ioDescription(status)). Check whether another helper owns the keyboard.") }
        device = selected
        removed = false
        leds = ledElements(selected)
        IOHIDDeviceRegisterRemovalCallback(selected, { context, _, _ in
            guard let context else { return }
            let owner = Unmanaged<Helper>.fromOpaque(context).takeUnretainedValue()
            owner.removed = true
            owner.setListeningOff()
            emit(["event": "device_removed"])
        }, Unmanaged.passUnretained(self).toOpaque())
        reportBufferSize = min(4096, max(64, (property(selected, kIOHIDMaxInputReportSizeKey) as? NSNumber)?.intValue ?? 64))
        reportBuffer = .allocate(capacity: reportBufferSize)
        IOHIDDeviceRegisterInputReportCallback(selected, reportBuffer!, reportBufferSize, { context, result, _, type, reportID, report, length in
            guard let context, result == kIOReturnSuccess, type == kIOHIDReportTypeInput else { return }
            let owner = Unmanaged<Helper>.fromOpaque(context).takeUnretainedValue()
            owner.handleReport(reportID: reportID, report: report, count: length)
        }, Unmanaged.passUnretained(self).toOpaque())
        IOHIDDeviceScheduleWithRunLoop(selected, CFRunLoopGetMain(), CFRunLoopMode.commonModes.rawValue)
        return describe(selected)
    }

    func activeDevice() throws -> IOHIDDevice {
        guard let target = device, !removed else { throw failure("device_not_open", "Open a connected K10 Pro before using this operation") }
        return target
    }

    static func validateSymbolElements(_ fields: [[String: Any]]) throws {
        guard fields.count == 3, Set(fields.compactMap { $0["usage"] as? Int }) == Set([3, 4, 5]) else {
            throw failure("led_elements_missing", "The selected interface lacks Scroll Lock, Compose, or Kana LED output elements")
        }
        guard Set(fields.compactMap { $0["report_id"] as? Int }).count == 1 else {
            throw failure("led_report_mismatch", "Symbol LED elements must share one output report")
        }
        guard fields.allSatisfy({ $0["report_size"] as? Int == 1 && $0["report_count"] as? Int == 1 &&
            $0["logical_min"] as? Int == 0 && $0["logical_max"] as? Int == 1 }) else {
            throw failure("led_descriptor_unsupported", "Symbol LEDs must each be one Boolean output bit")
        }
    }

    func writeSymbol(_ value: Int, method: String = "individual") throws -> [String: Any] {
        guard (0...7).contains(value) else { throw failure("invalid_argument", "symbol value must be 0...7") }
        guard ["individual", "multiple", "transaction"].contains(method) else {
            throw failure("invalid_argument", "symbol method must be individual, multiple, or transaction")
        }
        let target = try activeDevice()
        let elements = [3, 4, 5].compactMap { leds[$0] }
        try Self.validateSymbolElements(elements.map(describeLEDElement))
        let outputs = elements.enumerated().map { index, element in
            IOHIDValueCreateWithIntegerValue(kCFAllocatorDefault, element, 0, (value >> index) & 1)
        }
        // Submit only usages 3/4/5. Caps and Num Lock remain owned by macOS.
        // A successful host API call is not proof of Bluetooth transmission.
        if method == "individual" {
            for (element, output) in zip(elements, outputs) {
                let status = IOHIDDeviceSetValue(target, element, output)
                guard status == kIOReturnSuccess else {
                    throw failure("symbol_write_failed", "Usage \(IOHIDElementGetUsage(element)): \(ioDescription(status)); earlier bits may have been issued")
                }
            }
        } else if method == "multiple" {
            let values = Dictionary(uniqueKeysWithValues: zip(elements, outputs))
            let status = IOHIDDeviceSetValueMultiple(target, values as CFDictionary)
            guard status == kIOReturnSuccess else { throw failure("symbol_write_failed", ioDescription(status)) }
        } else {
            guard let transaction = IOHIDTransactionCreate(kCFAllocatorDefault, target, .output, 0) else {
                throw failure("symbol_transaction_failed", "Could not create the output transaction")
            }
            for (element, output) in zip(elements, outputs) {
                IOHIDTransactionAddElement(transaction, element)
                IOHIDTransactionSetValue(transaction, element, output, 0)
            }
            let status = IOHIDTransactionCommit(transaction)
            guard status == kIOReturnSuccess else { throw failure("symbol_write_failed", ioDescription(status)) }
        }
        return ["value": value, "method": method, "report_issued": true, "firmware_acknowledged": false]
    }

    func raw(_ request: [String: Any]) throws -> [String: Any] {
        let target = try activeDevice()
        guard transport(target).caseInsensitiveCompare("USB") == .orderedSame else { throw failure("usb_required", "Raw HID is available only on an explicitly opened USB connection") }
        guard let numbers = request["bytes"] as? [NSNumber], numbers.count == 32,
              numbers.allSatisfy({ CFGetTypeID($0) != CFBooleanGetTypeID() && $0.doubleValue == Double($0.intValue) && (0...255).contains($0.intValue) }) else {
            throw failure("invalid_argument", "bytes must be a 32-byte array")
        }
        let bytes = numbers.map { UInt8($0.intValue) }
        guard bytes[0] == 0xC7, (2...30).contains(Int(bytes[1])) else { throw failure("invalid_raw_packet", "Only Orca LED prefix 0xC7 and a 2...30 byte packet are allowed") }
        guard (property(target, kIOHIDPrimaryUsagePageKey) as? NSNumber)?.intValue == 0xFF60 else {
            throw failure("raw_interface_required", "Open the K10 Pro USB Raw HID interface (usage page 0xFF60) by registry_id")
        }
        let status = bytes.withUnsafeBufferPointer { IOHIDDeviceSetReport(target, kIOHIDReportTypeOutput, 0, $0.baseAddress!, $0.count) }
        guard status == kIOReturnSuccess else { throw failure("raw_write_failed", ioDescription(status)) }
        return ["bytes_written": bytes.count, "firmware_acknowledged": false]
    }

    static func crc8(_ bytes: [UInt8]) -> UInt8 {
        var crc: UInt8 = 0
        for byte in bytes {
            crc ^= byte
            for _ in 0..<8 { crc = (crc & 0x80) != 0 ? (crc &<< 1) ^ 0x07 : crc &<< 1 }
        }
        return crc
    }

    static func decodeQueryReply(_ bytes: [UInt8]) -> [String: Any]? {
        guard bytes.count == 32, Array(bytes.prefix(4)) == [0xC7, 0x4F, 0x4C, 1], bytes[6] <= 1,
              bytes[8..<20].allSatisfy({ $0 <= 7 }) else { return nil }
        return ["firmware_verified": true, "version": 1,
                "applied_counter": Int(bytes[4]) | (Int(bytes[5]) << 8),
                "mode_active": bytes[6] == 1, "brightness": Int(bytes[7]),
                "states": bytes[8..<20].map(Int.init), "transport": "USB",
                "theme_protocol": Int(bytes[20]), "theme_supported": (1...2).contains(bytes[20])]
    }

    func query() throws -> [String: Any] {
        let bytes: [UInt8] = [0xC7, 2, 0x15, Self.crc8([0x15])] + Array(repeating: 0, count: 28)
        return try exchangeUSB(bytes: bytes, kind: "firmware")
    }

    static func validateVIARead(_ bytes: [UInt8]) throws {
        guard bytes.count == 32 else { throw failure("invalid_argument", "VIA read requires exactly 32 bytes") }
        let paddingStart: Int
        switch bytes[0] {
        case 0x01, 0x0C, 0x0D, 0x11: paddingStart = 1
        case 0x02:
            guard [0x02, 0x04].contains(bytes[1]) else { throw failure("via_read_not_allowed", "Only layout options and firmware version keyboard getters are allowed") }
            paddingStart = 2
        case 0x08:
            guard [0x02, 0x03].contains(bytes[1]), (1...4).contains(bytes[2]) else { throw failure("via_read_not_allowed", "Only RGBLIGHT/RGB_MATRIX getter fields 1...4 are allowed") }
            paddingStart = 3
        case 0x0E, 0x12:
            guard (1...28).contains(bytes[3]) else { throw failure("invalid_argument", "VIA buffer read size must be 1...28") }
            paddingStart = 4
        default: throw failure("via_read_not_allowed", "This VIA command is not in the read-only allowlist")
        }
        guard bytes[paddingStart...].allSatisfy({ $0 == 0 }) else { throw failure("via_read_not_allowed", "Unused VIA request bytes must all be zero") }
    }

    static func decodeVIAReadReply(_ bytes: [UInt8], request: [UInt8]) throws -> [String: Any]? {
        guard bytes.count == 32 else { return nil }
        if bytes[0] == 0xFF { throw failure("via_unhandled", "Keyboard reported that this VIA read command is unsupported") }
        let prefixSize: Int
        switch request[0] {
        case 0x02: prefixSize = 2
        case 0x08: prefixSize = 3
        case 0x0E, 0x12: prefixSize = 4
        default: prefixSize = 1
        }
        guard bytes.prefix(prefixSize) == request.prefix(prefixSize) else { return nil }
        return ["bytes": bytes.map(Int.init), "hex": bytes.map { String(format: "%02x", $0) }.joined(),
                "read_only": true, "transport": "USB"]
    }

    func viaRead(_ request: [String: Any]) throws -> [String: Any] {
        guard let numbers = request["bytes"] as? [NSNumber], numbers.count == 32,
              numbers.allSatisfy({ CFGetTypeID($0) != CFBooleanGetTypeID() && $0.doubleValue == Double($0.intValue) && (0...255).contains($0.intValue) }) else {
            throw failure("invalid_argument", "bytes must be a 32-byte array")
        }
        let bytes = numbers.map { UInt8($0.intValue) }
        try Self.validateVIARead(bytes)
        return try exchangeUSB(bytes: bytes, kind: "via")
    }

    static func validateVIAWrite(_ bytes: [UInt8]) throws {
        guard bytes.count == 32 else { throw failure("invalid_argument", "VIA write requires exactly 32 bytes") }
        let paddingStart: Int
        switch bytes[0] {
        case 0x13, 0x0F:
            let length = Int(bytes[3])
            let offset = (Int(bytes[1]) << 8) | Int(bytes[2])
            guard (1...28).contains(length), offset + length <= 65536 else {
                throw failure("invalid_argument", "VIA restore buffer range is invalid")
            }
            paddingStart = 4 + length
        case 0x03:
            guard bytes[1] == 0x02 else { throw failure("via_write_not_allowed", "Only layout options may be restored with the keyboard setter") }
            paddingStart = 6
        case 0x07:
            guard [0x02, 0x03].contains(bytes[1]), (1...4).contains(bytes[2]) else {
                throw failure("via_write_not_allowed", "Only RGBLIGHT/RGB_MATRIX restore fields 1...4 are allowed")
            }
            paddingStart = bytes[2] == 4 ? 5 : 4
        case 0x09:
            guard [0x02, 0x03].contains(bytes[1]) else { throw failure("via_write_not_allowed", "Only RGBLIGHT/RGB_MATRIX settings may be saved") }
            paddingStart = 2
        default: throw failure("via_write_not_allowed", "This VIA command is not in the configuration-restore allowlist")
        }
        guard bytes[paddingStart...].allSatisfy({ $0 == 0 }) else {
            throw failure("via_write_not_allowed", "Unused VIA restore bytes must all be zero")
        }
    }

    func viaWrite(_ request: [String: Any]) throws -> [String: Any] {
        guard let numbers = request["bytes"] as? [NSNumber], numbers.count == 32,
              numbers.allSatisfy({ CFGetTypeID($0) != CFBooleanGetTypeID() && $0.doubleValue == Double($0.intValue) && (0...255).contains($0.intValue) }) else {
            throw failure("invalid_argument", "bytes must be a 32-byte array")
        }
        let bytes = numbers.map { UInt8($0.intValue) }
        try Self.validateVIAWrite(bytes)
        return try exchangeUSB(bytes: bytes, kind: "via-write")
    }

    func exchangeUSB(bytes: [UInt8], kind: String) throws -> [String: Any] {
        let target = try activeDevice()
        guard transport(target).caseInsensitiveCompare("USB") == .orderedSame else {
            throw failure("usb_required", "This request requires an explicitly opened USB connection")
        }
        guard (property(target, kIOHIDPrimaryUsagePageKey) as? NSNumber)?.intValue == 0xFF60 else {
            throw failure("raw_interface_required", "Open the K10 Pro USB Raw HID interface (usage page 0xFF60) by registry_id")
        }
        let transaction = USBQuery(registry: registryID(target), bytes: bytes, kind: kind)
        pendingQuery = transaction
        defer { pendingQuery = nil }
        // Retain the output buffer until the asynchronous write callback, even
        // if our response deadline expires or the device disconnects first.
        let context = Unmanaged.passRetained(transaction).toOpaque()
        let deadline = ProcessInfo.processInfo.systemUptime + 1.0
        let status = IOHIDDeviceSetReportWithCallback(target, kIOHIDReportTypeOutput, 0, transaction.report, 32, 1000, { context, status, _, _, _, _, _ in
            guard let context else { return }
            let transaction = Unmanaged<USBQuery>.fromOpaque(context).takeRetainedValue()
            if status != kIOReturnSuccess { transaction.error = failure("query_write_failed", ioDescription(status)) }
        }, context)
        guard status == kIOReturnSuccess else {
            Unmanaged<USBQuery>.fromOpaque(context).release()
            throw failure("query_write_failed", ioDescription(status))
        }
        while transaction.response == nil && transaction.error == nil && !removed && !stopping {
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            if remaining <= 0 { break }
            CFRunLoopRunInMode(.defaultMode, min(0.02, remaining), true)
        }
        if let error = transaction.error { throw error }
        guard !removed, !stopping else { throw failure("query_interrupted", "Device disconnected or helper stopped during firmware query") }
        guard let response = transaction.response else {
            throw failure(kind.hasPrefix("via") ? (kind == "via-write" ? "via_write_timeout" : "via_read_timeout") : "firmware_query_timeout", kind.hasPrefix("via") ?
                          "No matching VIA reply arrived within 1000 ms" :
                          "No valid Orca LED firmware v1 reply arrived within 1000 ms; a successful write is not firmware verification")
        }
        return response
    }

    static func minimizedWindowStatus(trusted: Bool, windowCount: Int?, minimized: Bool?) -> [String: Any] {
        guard trusted else { return ["is_minimized": NSNull(), "window_state_reason": "accessibility_permission_required"] }
        guard let count = windowCount else { return ["is_minimized": NSNull(), "window_state_reason": "window_list_unavailable"] }
        guard count == 1 else {
            return ["is_minimized": NSNull(), "window_state_reason": count == 0 ? "window_not_found" : "multiple_windows_unsupported"]
        }
        guard let minimized else { return ["is_minimized": NSNull(), "window_state_reason": "minimized_state_unavailable"] }
        return ["is_minimized": minimized, "window_state_reason": "confirmed_single_window"]
    }

    func frontmost() -> [String: Any] {
        guard let app = NSWorkspace.shared.frontmostApplication else {
            return ["pid": NSNull(), "bundle_id": NSNull(), "is_minimized": NSNull(), "window_state_reason": "frontmost_app_unavailable"]
        }
        var result: [String: Any] = ["pid": Int(app.processIdentifier), "bundle_id": app.bundleIdentifier as Any? ?? NSNull(),
                                     "is_minimized": NSNull(), "window_state_reason": "not_orca"]
        guard app.bundleIdentifier == "com.stablyai.orca" else { return result }
        let trusted = AXIsProcessTrusted()
        let windows = trusted ? axValue(AXUIElementCreateApplication(app.processIdentifier), kAXWindowsAttribute) as? [AXUIElement] : nil
        let minimized = windows?.count == 1 ? axValue(windows![0], kAXMinimizedAttribute) as? Bool : nil
        result.merge(Self.minimizedWindowStatus(trusted: trusted, windowCount: windows?.count, minimized: minimized)) { _, new in new }
        return result
    }

    func runningApp(_ request: [String: Any]) throws -> NSRunningApplication {
        if request["pid"] != nil {
            let pid = try integer(request, "pid")
            guard pid > 0, pid <= Int(Int32.max), let app = NSRunningApplication(processIdentifier: pid_t(pid)), !app.isTerminated else {
                throw failure("app_not_running", "The requested PID is not a running application")
            }
            if let bundle = request["bundle_id"] as? String, app.bundleIdentifier != bundle { throw failure("app_mismatch", "PID and bundle_id refer to different applications") }
            return app
        }
        guard let bundle = request["bundle_id"] as? String, !bundle.isEmpty else { throw failure("invalid_argument", "Specify a running app pid or bundle_id") }
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundle)
        guard apps.count == 1, let app = apps.first else { throw failure(apps.isEmpty ? "app_not_running" : "ambiguous_app", "Expected exactly one running application for bundle_id") }
        return app
    }

    func windowForActivation(_ pid: pid_t) throws -> AXUIElement {
        let app = AXUIElementCreateApplication(pid)
        if let remembered = rememberedWindows[pid], axValue(remembered, kAXRoleAttribute) != nil { return remembered }
        if let focused = axElement(app, kAXFocusedWindowAttribute) { return focused }
        if let main = axElement(app, kAXMainWindowAttribute) { return main }
        let windows = axValue(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
        guard windows.count == 1, let window = windows.first else {
            throw failure(windows.isEmpty ? "window_not_found" : "ambiguous_window", "Could not identify one target application window")
        }
        return window
    }

    static func requireSingleWindow(_ request: [String: Any]) throws -> Bool {
        let single = try bool(request, "single_window", default: false)
        if request["window_id"] != nil && !single {
            throw failure("window_id_requires_single_window", "Electron window_id cannot identify an AX window; explicitly require single_window:true")
        }
        return single
    }

    func soleWindow(_ pid: pid_t) throws -> AXUIElement {
        let windows = axValue(AXUIElementCreateApplication(pid), kAXWindowsAttribute) as? [AXUIElement] ?? []
        guard windows.count == 1, let window = windows.first else {
            throw failure(windows.isEmpty ? "window_not_found" : "multiple_windows_unsupported", "single_window requires exactly one AX window; observed \(windows.count)")
        }
        return window
    }

    func activate(_ request: [String: Any]) throws -> [String: Any] {
        let single = try Self.requireSingleWindow(request)
        try requireAccessibility()
        try requireActiveSession()
        let app = try runningApp(request)
        let window = try single ? soleWindow(app.processIdentifier) : windowForActivation(app.processIdentifier)
        if (axValue(window, kAXMinimizedAttribute) as? Bool) == true {
            let result = AXUIElementSetAttributeValue(window, kAXMinimizedAttribute as CFString, kCFBooleanFalse)
            guard result == .success else { throw failure("restore_failed", "Window restoration failed with AX error \(result.rawValue)") }
        }
        guard app.activate(options: []) else { throw failure("activation_failed", "macOS rejected application activation") }
        let raise = AXUIElementPerformAction(window, kAXRaiseAction as CFString)
        guard raise == .success else { throw failure("raise_failed", "Application activated, but target window could not be raised (AX \(raise.rawValue))") }
        rememberedWindows[app.processIdentifier] = window
        return ["pid": Int(app.processIdentifier), "activation_requested": true,
                "frontmost": NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier]
    }

    static func confirmAsynchronousState(timeout: TimeInterval, cancelled: () -> Bool = { false }, read: () -> Bool) -> Bool {
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        while !cancelled() {
            if read() { return true }
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            if remaining <= 0 { return false }
            let interval = min(0.02, remaining)
            if CFRunLoopRunInMode(.defaultMode, interval, true) == .finished {
                // Standalone diagnostics may not have an attached run-loop source.
                Thread.sleep(forTimeInterval: interval)
            }
        }
        return false
    }

    func minimize(_ request: [String: Any]) throws -> [String: Any] {
        let single = try Self.requireSingleWindow(request)
        try requireAccessibility()
        try requireActiveSession()
        let app = try runningApp(request)
        let sole = try single ? soleWindow(app.processIdentifier) : nil
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier else { throw failure("app_not_frontmost", "Refusing to minimize a background application") }
        guard let window = axElement(AXUIElementCreateApplication(app.processIdentifier), kAXFocusedWindowAttribute) else { throw failure("window_not_found", "No focused application window is available") }
        if let sole, !CFEqual(sole, window) { throw failure("focused_window_mismatch", "The focused window is not the sole verified application window") }
        let result = AXUIElementSetAttributeValue(window, kAXMinimizedAttribute as CFString, kCFBooleanTrue)
        guard result == .success else { throw failure("minimize_failed", "Window minimization failed with AX error \(result.rawValue)") }
        rememberedWindows[app.processIdentifier] = window
        let began = ProcessInfo.processInfo.systemUptime
        // Electron/macOS can acknowledge the setter before its minimize
        // animation updates AXMinimized. Confirm the resulting state instead
        // of treating the first immediate false observation as a failure.
        let confirmed = Self.confirmAsynchronousState(timeout: 1.0, cancelled: { self.stopping }) {
            (axValue(window, kAXMinimizedAttribute) as? Bool) == true
        }
        guard confirmed else {
            throw failure(stopping ? "minimize_interrupted" : "minimize_unconfirmed",
                          "macOS accepted minimization, but AXMinimized did not confirm it within 1000 ms")
        }
        return ["pid": Int(app.processIdentifier), "minimized": true,
                "confirmation_wait_ms": Int((ProcessInfo.processInfo.systemUptime - began) * 1000)]
    }

    func orcaUI(_ request: [String: Any]) throws -> [String: Any] {
        try requireAccessibility()
        var targetRequest = request
        targetRequest["bundle_id"] = "com.stablyai.orca"
        let app = try runningApp(targetRequest)
        let application = AXUIElementCreateApplication(app.processIdentifier)
        let roots = axValue(application, kAXWindowsAttribute) as? [AXUIElement] ?? []
        var lists: [[String: Any]] = []
        var candidateLists: [[String: Any]] = []
        var visited = 0
        let scalarAttributes = ["AXRole", "AXSubrole", "AXRoleDescription", "AXIdentifier", "AXDOMIdentifier", "AXTitle", "AXDescription", "AXHelp", "AXSelected", "AXFocused", "AXValue", "AXDOMClassList"]

        func attributes(_ element: AXUIElement) -> [String: Any] {
            var result: [String: Any] = [:]
            for name in scalarAttributes {
                if let value = axValue(element, name) {
                    if let string = value as? String { result[name] = String(string.prefix(512)) }
                    else if let number = value as? NSNumber { result[name] = number }
                    else if let strings = value as? [String] { result[name] = strings.map { String($0.prefix(128)) } }
                }
            }
            return result
        }

        func collectMenuItems(_ element: AXUIElement, depth: Int, into rows: inout [[String: Any]]) {
            guard depth <= 8, rows.count < 100 else { return }
            let role = axValue(element, kAXRoleAttribute) as? String ?? ""
            if role == "AXMenuItem" || role == "AXRow" || role == "AXOption" {
                var item = attributes(element)
                let children = axValue(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
                item["children"] = children.prefix(16).map(attributes)
                rows.append(item)
                return
            }
            for child in axValue(element, kAXChildrenAttribute) as? [AXUIElement] ?? [] {
                collectMenuItems(child, depth: depth + 1, into: &rows)
            }
        }

        func visit(_ element: AXUIElement, depth: Int, windowIndex: Int) {
            guard depth <= 18, visited < 2000 else { return }
            visited += 1
            let attrs = attributes(element)
            let role = attrs["AXRole"] as? String ?? ""
            let subrole = attrs["AXSubrole"] as? String ?? ""
            let description = [attrs["AXTitle"], attrs["AXDescription"], attrs["AXHelp"]].compactMap { $0 as? String }.joined(separator: " ")
            let isList = ["AXList", "AXListBox", "AXOutline"].contains(role) || subrole.lowercased().contains("listbox")
            let isWorktree = description.contains("워크트리") || description.lowercased().contains("worktree")
            if isList { candidateLists.append(["window_index": windowIndex, "attributes": attrs]) }
            if isList && isWorktree {
                var rows: [[String: Any]] = []
                collectMenuItems(element, depth: 0, into: &rows)
                lists.append(["window_index": windowIndex, "attributes": attrs, "rows": rows])
                return
            }
            for child in axValue(element, kAXChildrenAttribute) as? [AXUIElement] ?? [] {
                visit(child, depth: depth + 1, windowIndex: windowIndex)
            }
        }

        for (index, window) in roots.enumerated() { visit(window, depth: 0, windowIndex: index) }
        func menuTree(_ element: AXUIElement, depth: Int) -> [String: Any] {
            var result = attributes(element)
            if depth > 0 {
                result["children"] = (axValue(element, kAXChildrenAttribute) as? [AXUIElement] ?? []).prefix(64).map { menuTree($0, depth: depth - 1) }
            }
            return result
        }
        let menu = axElement(application, kAXMenuBarAttribute).map { menuTree($0, depth: 3) }
        return ["pid": Int(app.processIdentifier), "bundle_id": "com.stablyai.orca",
                "windows": roots.map(attributes), "worktree_lists": lists,
                "menu_bar": menu as Any? ?? NSNull(),
                "candidate_lists": candidateLists,
                "visited_elements": visited, "truncated": visited >= 2000]
    }

    func setListeningOff() {
        listening = false
        assignedMask = 0
        slotTracker.held.removeAll()
    }

    func listen(_ request: [String: Any]) throws -> [String: Any] {
        let enabled = try bool(request, "enabled", default: false)
        if !enabled { setListeningOff(); return ["enabled": false] }
        let target = try activeDevice()
        guard transport(target).caseInsensitiveCompare("Bluetooth") == .orderedSame else {
            throw failure("bluetooth_input_required", "This consumer report decoder currently supports the verified Bluetooth descriptor only")
        }
        guard CGPreflightListenEventAccess() else { throw failure("input_monitoring_permission_required", "Input Monitoring access is required; permissions does not request it.") }
        let mask = try integer(request, "assigned_mask", default: 0)
        guard (0...0xFFF).contains(mask) else { throw failure("invalid_argument", "assigned_mask must be 0...4095") }
        assignedMask = mask
        listening = true
        return ["enabled": true, "assigned_mask": mask, "device_exclusive": true,
                "input_protocol": "consumer-0x03e0-0x03eb", "event_tap": false,
                "hardware_verified": false]
    }

    static func slotsFromConsumerReport(reportID: UInt32, bytes: [UInt8]) -> Set<Int>? {
        guard reportID == 0x0A else { return nil }
        let payload: ArraySlice<UInt8>
        if bytes.count == 7 && bytes[0] == 0x0A { payload = bytes.dropFirst() }
        else if bytes.count == 6 { payload = bytes[...] }
        else { return nil }
        let values = Array(payload)
        var slots = Set<Int>()
        for offset in stride(from: 0, to: 6, by: 2) {
            let usage = Int(values[offset]) | (Int(values[offset + 1]) << 8)
            if (0x03E0...0x03EB).contains(usage) { slots.insert(usage - 0x03E0) }
        }
        return slots
    }

    func handleReport(reportID: UInt32, report: UnsafeMutablePointer<UInt8>, count: Int) {
        if let transaction = pendingQuery, let target = device, !removed,
           registryID(target) == transaction.registry, transport(target).caseInsensitiveCompare("USB") == .orderedSame,
           reportID == 0, count == 32 {
            let bytes = Array(UnsafeBufferPointer(start: report, count: count))
            if transaction.kind == "via-write" {
                if bytes[0] == 0xFF { transaction.error = failure("via_unhandled", "Keyboard reported that this VIA restore command is unsupported") }
                else if bytes == transaction.request {
                    transaction.response = ["bytes": bytes.map(Int.init), "write_accepted": true, "verification_required": true, "transport": "USB"]
                }
            } else if transaction.kind == "via" {
                do { transaction.response = try Self.decodeVIAReadReply(bytes, request: transaction.request) }
                catch let error as HelperFailure { transaction.error = error }
                catch { transaction.error = failure("via_reply_invalid", error.localizedDescription) }
            } else if let decoded = Self.decodeQueryReply(bytes) {
                transaction.response = decoded
            }
            return
        }
        // No general keyboard report logging or storage. Only decode the target
        // device's documented consumer report while explicitly enabled.
        guard listening, device != nil, !removed, reportID == 0x0A, count == 6 || count == 7 else { return }
        guard let current = Self.slotsFromConsumerReport(reportID: reportID, bytes: Array(UnsafeBufferPointer(start: report, count: count))) else { return }
        consumerReportsReceived += 1
        let active = sessionStatus()["active"] as? Bool == true
        // Track releases during lock so a held key does not replay on unlock.
        for slot in slotTracker.update(current, assignedMask: active ? assignedMask : 0) {
            emit(["event": "slot", "slot": slot])
        }
    }

    func closeDevice() {
        setListeningOff()
        if let target = device {
            IOHIDDeviceUnscheduleFromRunLoop(target, CFRunLoopGetMain(), CFRunLoopMode.commonModes.rawValue)
            IOHIDDeviceClose(target, IOOptionBits(kIOHIDOptionsTypeNone))
        }
        reportBuffer?.deallocate()
        reportBuffer = nil
        reportBufferSize = 0
        device = nil
        leds = [:]
        removed = false
    }

    func shutdown() {
        guard !stopping else { return }
        stopping = true
        closeDevice()
        CFRunLoopStop(CFRunLoopGetMain())
    }

    func dispatch(_ request: [String: Any]) throws -> [String: Any] {
        guard let op = request["op"] as? String else { throw failure("invalid_request", "op is required") }
        if pendingQuery != nil && !["ping", "permissions", "session", "frontmost", "input-status"].contains(op) {
            throw failure("query_in_progress", "Wait for the pending firmware query before issuing another operation")
        }
        switch op {
        case "devices": return ["devices": try discover().map(describe)]
        case "permissions": return permissions()
        case "session": return sessionStatus()
        case "request-permissions":
            if try bool(request, "accessibility", default: false) {
                _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
            }
            if try bool(request, "input_monitoring", default: false) { _ = CGRequestListenEventAccess() }
            return permissions()
        case "open": return try open(request)
        case "close": closeDevice(); return ["closed": true]
        case "symbol":
            if let method = request["method"], !(method is String) { throw failure("invalid_argument", "symbol method must be a string") }
            return try writeSymbol(integer(request, "value"), method: request["method"] as? String ?? "individual")
        case "raw": return try raw(request)
        case "query": return try query()
        case "via-read": return try viaRead(request)
        case "via-write": return try viaWrite(request)
        case "frontmost": return frontmost()
        case "activate": return try activate(request)
        case "minimize": return try minimize(request)
        case "orca-ui": return try orcaUI(request)
        case "listen": return try listen(request)
        case "input-status": return ["enabled": listening, "assigned_mask": assignedMask,
                                      "consumer_reports_received": consumerReportsReceived,
                                      "held_slots": slotTracker.held.sorted(), "device_exclusive": true]
        case "ping": return ["protocol": 1, "helper": "orca-keychron-helper"]
        default: throw failure("unknown_op", "Unknown operation: \(op)")
        }
    }

    func process(_ line: String) {
        var id: Any = NSNull()
        do {
            guard line.utf8.count <= 65536 else { throw failure("request_too_large", "Maximum JSONL request size is 65536 bytes") }
            guard let request = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] else { throw failure("invalid_request", "Request must be a JSON object") }
            id = request["id"] ?? NSNull()
            if !(id is String) && !(id is NSNumber) && !(id is NSNull) { throw failure("invalid_request", "id must be a string, number, or null") }
            let result = try dispatch(request)
            emit(["id": id, "ok": true, "result": result])
        } catch let error as HelperFailure {
            emit(["id": id, "ok": false, "error": ["code": error.code, "message": error.message]])
        } catch {
            emit(["id": id, "ok": false, "error": ["code": "invalid_json", "message": error.localizedDescription]])
        }
    }

    func serve() {
        signal(SIGPIPE, SIG_IGN)
        for sig in [SIGINT, SIGTERM] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in self?.shutdown() }
            source.resume()
            signalSources.append(source)
        }
        DispatchQueue.global(qos: .userInitiated).async {
            while let line = readLine(strippingNewline: true) {
                DispatchQueue.main.async { [weak self] in self?.process(line) }
            }
            DispatchQueue.main.async { [weak self] in self?.shutdown() }
        }
        let keepAlive = Timer.scheduledTimer(withTimeInterval: 3600, repeats: true) { _ in }
        CFRunLoopRun()
        keepAlive.invalidate()
    }
}

func selfTest() -> Bool {
    var checks = 0
    func verify(_ condition: Bool, _ label: String) -> Bool {
        checks += 1
        if !condition { emit(["test": label, "ok": false]) }
        return condition
    }
    var passed = true
    let symbolFields: [[String: Any]] = (3...5).map { ["usage": $0, "report_id": 1, "report_size": 1, "report_count": 1, "logical_min": 0, "logical_max": 1] }
    do { try Helper.validateSymbolElements(symbolFields); passed = verify(true, "symbol-valid-single-report") && passed }
    catch { passed = verify(false, "symbol-valid-single-report") && passed }
    for (field, replacement, label) in [("report_id", 2, "mixed-reports"), ("report_size", 8, "byte-field"),
                                      ("report_count", 2, "array-field"), ("logical_min", -1, "negative-min"),
                                      ("logical_max", 2, "nonboolean-max"), ("usage", 2, "caps-lock-forbidden")] {
        var fields = symbolFields; fields[0][field] = replacement
        do { try Helper.validateSymbolElements(fields); passed = verify(false, "symbol-reject-\(label)") && passed }
        catch { passed = verify(true, "symbol-reject-\(label)") && passed }
    }
    for slot in 0..<12 {
        let usage = 0x03E0 + slot
        let bytes = [UInt8(usage & 255), UInt8(usage >> 8), 0, 0, 0, 0]
        passed = verify(Helper.slotsFromConsumerReport(reportID: 0x0A, bytes: bytes) == [slot], "slot-\(slot)-payload") && passed
        passed = verify(Helper.slotsFromConsumerReport(reportID: 0x0A, bytes: [0x0A] + bytes) == [slot], "slot-\(slot)-report-id") && passed
    }
    passed = verify(Helper.slotsFromConsumerReport(reportID: 0x0A, bytes: [0, 0, 0, 0, 0, 0]) == [], "release") && passed
    passed = verify(Helper.slotsFromConsumerReport(reportID: 0x0A, bytes: [0xE0, 3, 0xE5, 3, 0xEB, 3]) == [0, 5, 11], "three-held-slots") && passed
    passed = verify(Helper.slotsFromConsumerReport(reportID: 0x01, bytes: [0xE0, 3, 0, 0, 0, 0]) == nil, "ignore-keyboard-report") && passed
    passed = verify(Helper.slotsFromConsumerReport(reportID: 0x0A, bytes: [0xE0, 3]) == nil, "reject-short-report") && passed
    passed = verify(Helper.slotsFromConsumerReport(reportID: 0x0A, bytes: [0x01, 0xE0, 3, 0, 0, 0, 0]) == nil, "reject-wrong-prefix") && passed
    passed = verify(Helper.slotsFromConsumerReport(reportID: 0x0A, bytes: [0xCD, 0, 0, 0, 0, 0]) == [], "ignore-normal-consumer-key") && passed
    var tracker = ConsumerSlotTracker()
    passed = verify(tracker.update([0], assignedMask: 1) == [0], "initial-press") && passed
    passed = verify(tracker.update([0], assignedMask: 1) == [], "hold-is-not-repeated") && passed
    passed = verify(tracker.update([], assignedMask: 1) == [], "release-rearms") && passed
    passed = verify(tracker.update([0], assignedMask: 1) == [0], "second-press-after-release") && passed
    passed = verify(tracker.update([0, 1], assignedMask: 1) == [], "unassigned-slot-ignored") && passed
    passed = verify(tracker.update([0, 1], assignedMask: 3) == [], "mask-change-does-not-retrigger-held-key") && passed
    passed = verify(tracker.update([0, 1, 11], assignedMask: 0xFFF) == [11], "additional-held-slot") && passed
    var reply: [UInt8] = [0xC7, 0x4F, 0x4C, 1, 0x34, 0x12, 1, 90] + Array(repeating: 2, count: 12) + Array(repeating: 0, count: 12)
    let decoded = Helper.decodeQueryReply(reply)
    passed = verify(decoded?["firmware_verified"] as? Bool == true && decoded?["applied_counter"] as? Int == 0x1234, "query-valid-identity-and-counter") && passed
    passed = verify(decoded?["theme_protocol"] as? Int == 0 && decoded?["theme_supported"] as? Bool == false, "query-legacy-has-no-theme-support") && passed
    reply[20] = 1
    passed = verify(Helper.decodeQueryReply(reply)?["theme_supported"] as? Bool == true, "query-theme-extension-supported") && passed
    reply[20] = 2
    passed = verify(Helper.decodeQueryReply(reply)?["theme_supported"] as? Bool == true, "query-seen-completion-theme-supported") && passed
    reply[20] = 3
    passed = verify(Helper.decodeQueryReply(reply)?["theme_supported"] as? Bool == false, "query-future-theme-extension-not-assumed") && passed
    reply[20] = 0
    passed = verify(Helper.decodeQueryReply(Array(reply.dropLast())) == nil, "query-reject-truncated") && passed
    reply[3] = 2
    passed = verify(Helper.decodeQueryReply(reply) == nil, "query-reject-other-version") && passed
    reply[3] = 1; reply[8] = 7
    passed = verify(Helper.decodeQueryReply(reply)?["states"] as? [Int] == [7] + Array(repeating: 2, count: 11), "query-accepts-seen-completion-state") && passed
    reply[8] = 8
    passed = verify(Helper.decodeQueryReply(reply) == nil, "query-reject-out-of-range-state") && passed
    passed = verify(Helper.crc8([0x15]) == 0x6B, "query-crc-vector") && passed
    do {
        _ = try Helper.requireSingleWindow(["window_id": 2])
        passed = verify(false, "electron-window-id-requires-single-window") && passed
    } catch { passed = verify(true, "electron-window-id-requires-single-window") && passed }
    passed = verify((try? Helper.requireSingleWindow(["window_id": 2, "single_window": true])) == true, "explicit-single-window-policy") && passed
    let sessionCases: [(Bool?, Bool?, Bool?, Bool?, Bool, String)] = [
        (true, true, nil, false, true, "unlocked-with-absent-cg-lock-key"),
        (true, true, nil, true, false, "io-console-lock-blocked"),
        (true, true, true, false, false, "cg-lock-overrides-unlocked-io-state"),
        (true, true, false, true, false, "io-lock-overrides-unlocked-cg-state"),
        (true, true, nil, nil, false, "both-lock-signals-unknown-blocked"),
        (false, true, nil, false, false, "remote-session-blocked"),
        (true, nil, nil, false, false, "unknown-login-blocked"),
        (nil, true, false, false, false, "unknown-console-blocked"),
    ]
    for (onConsole, login, cgLocked, ioLocked, expected, label) in sessionCases {
        passed = verify(Helper.sessionDecision(onConsole: onConsole, loginDone: login, sessionLocked: cgLocked, consoleLocked: ioLocked)["active"] as? Bool == expected, label) && passed
    }
    for command: UInt8 in [0x01, 0x0C, 0x0D, 0x11] {
        do { try Helper.validateVIARead([command] + Array(repeating: 0, count: 31)); passed = verify(true, "via-allowed-\(command)") && passed }
        catch { passed = verify(false, "via-allowed-\(command)") && passed }
    }
    for command: UInt8 in [0x03, 0x05, 0x06, 0x07, 0x09, 0x0A, 0x0B, 0x0F, 0x10, 0x13] {
        do { try Helper.validateVIARead([command] + Array(repeating: 0, count: 31)); passed = verify(false, "via-blocked-mutation-\(command)") && passed }
        catch { passed = verify(true, "via-blocked-mutation-\(command)") && passed }
    }
    let viaRequest: [UInt8] = [0x12, 0, 16, 28] + Array(repeating: 0, count: 28)
    let viaReply: [UInt8] = [0x12, 0, 16, 28] + Array(repeating: 42, count: 28)
    do {
        try Helper.validateVIARead(viaRequest)
        passed = verify(try Helper.decodeVIAReadReply(viaReply, request: viaRequest)?["read_only"] as? Bool == true, "via-buffer-roundtrip") && passed
        var mismatched = viaReply; mismatched[2] = 17
        passed = verify(try Helper.decodeVIAReadReply(mismatched, request: viaRequest) == nil, "via-reject-offset-mismatch") && passed
    } catch { passed = verify(false, "via-buffer-roundtrip") && passed }
    do { try Helper.validateVIARead([0x01, 1] + Array(repeating: 0, count: 30)); passed = verify(false, "via-blocked-nonzero-padding") && passed }
    catch { passed = verify(true, "via-blocked-nonzero-padding") && passed }
    for command: UInt8 in [0x0A, 0x0B, 0x06, 0x10] {
        do { try Helper.validateVIAWrite([command] + Array(repeating: 0, count: 31)); passed = verify(false, "via-restore-blocks-reset-\(command)") && passed }
        catch { passed = verify(true, "via-restore-blocks-reset-\(command)") && passed }
    }
    do {
        try Helper.validateVIAWrite([0x13, 0, 0, 28] + Array(repeating: 42, count: 28))
        try Helper.validateVIAWrite([0x07, 3, 4, 100, 200] + Array(repeating: 0, count: 27))
        passed = verify(true, "via-restore-valid-keymap-and-color") && passed
    } catch { passed = verify(false, "via-restore-valid-keymap-and-color") && passed }
    var observations = 0
    passed = verify(Helper.confirmAsynchronousState(timeout: 0.5) {
        observations += 1
        return observations >= 3
    }, "asynchronous-window-state-confirms-after-initial-false") && passed
    passed = verify(!Helper.confirmAsynchronousState(timeout: 0.02) { false }, "asynchronous-window-state-times-out") && passed
    passed = verify(!Helper.confirmAsynchronousState(timeout: 0.5, cancelled: { true }) { true }, "asynchronous-window-state-cancelled") && passed
    passed = verify(Helper.minimizedWindowStatus(trusted: true, windowCount: 1, minimized: true)["is_minimized"] as? Bool == true, "frontmost-preserves-minimized-orca") && passed
    passed = verify(Helper.minimizedWindowStatus(trusted: true, windowCount: 1, minimized: false)["is_minimized"] as? Bool == false, "frontmost-confirms-visible-orca") && passed
    for (trusted, count, value, label): (Bool, Int?, Bool?, String) in [
        (false, 1, false, "frontmost-permission-denied-is-unknown"),
        (true, nil, false, "frontmost-window-list-unavailable-is-unknown"),
        (true, 2, false, "frontmost-multiple-windows-is-unknown"),
        (true, 1, nil, "frontmost-missing-minimized-state-is-unknown"),
    ] {
        passed = verify(Helper.minimizedWindowStatus(trusted: trusted, windowCount: count, minimized: value)["is_minimized"] is NSNull, label) && passed
    }
    emit(["self_test": passed ? "passed" : "failed", "checks": checks])
    return passed
}

let helper = Helper()
switch CommandLine.arguments.dropFirst().first ?? "--menu" {
case "--menu": launchStatusApp(helper: helper)
case "--serve": helper.serve()
case "--devices": helper.process("{\"id\":1,\"op\":\"devices\"}"); helper.shutdown()
case "--permissions": helper.process("{\"id\":1,\"op\":\"permissions\"}"); helper.shutdown()
case "--request-permissions":
    helper.process("{\"id\":1,\"op\":\"request-permissions\",\"accessibility\":true,\"input_monitoring\":true}")
    helper.shutdown()
case "--self-test": exit(selfTest() ? 0 : 1)
default:
    FileHandle.standardError.write(Data("Usage: orca-keychron-helper --serve | --devices | --permissions | --request-permissions | --self-test\n".utf8))
    exit(CommandLine.arguments.count > 1 ? 2 : 0)
}
