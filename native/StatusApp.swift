import AppKit
import Foundation

// Launching the menu app opens Orca in local debugging mode (asking before it
// restarts a normally launched Orca) and then starts the bridge. JSONL --serve
// remains a separate mode of this same signed executable and permission identity.
final class StatusAppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    static let orcaApp = URL(fileURLWithPath: "/Applications/Orca.app")
    static let orcaBundleID = "com.stablyai.orca"
    static let cdpPort = 19347

    let helper: Helper
    let project: URL   // directory holding the orca_led package (PYTHONPATH)
    let dataRoot: URL  // bridge working directory; runtime/ lives here
    let runtime: URL
    let bundled: Bool
    var statusItem: NSStatusItem!
    var menu = NSMenu()
    var timer: Timer?
    var bridgeProcess: Process?
    var bridgeLog: FileHandle?
    var startedAt: TimeInterval = 0
    var brightness = 72
    var cdpEnabled = true
    var lastMessage = "연동이 중지되어 있습니다."
    var starting = false
    var stopping = false
    var restartAfterStop = false
    var quitting = false
    var checkingFirmware = false
    var preparingOrca = false
    var autoRetries = 0

    init(helper: Helper) {
        self.helper = helper
        // A source checkout keeps using its own runtime/ folder. A downloaded app
        // runs the Python package bundled in Resources and keeps state in
        // Application Support, so it works from /Applications.
        let parent = Bundle.main.bundleURL.deletingLastPathComponent()
        let candidate = parent.lastPathComponent == "build" ? parent.deletingLastPathComponent() : parent
        if FileManager.default.fileExists(atPath: candidate.appendingPathComponent("orca_led/__main__.py").path) {
            project = candidate
            dataRoot = candidate
            bundled = false
        } else {
            project = Bundle.main.resourceURL ?? Bundle.main.bundleURL.appendingPathComponent("Contents/Resources")
            dataRoot = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Orca LED", isDirectory: true)
            bundled = true
        }
        runtime = dataRoot.appendingPathComponent("runtime", isDirectory: true)
        super.init()
        if let data = try? Data(contentsOf: runtime.appendingPathComponent("menu-settings.json")),
           let settings = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let value = settings["brightness"] as? Int, (0...255).contains(value) { brightness = value }
            if let value = settings["cdp_enabled"] as? Bool { cdpEnabled = value }
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let bundle = Bundle.main.bundleIdentifier {
            let otherMenuApps = NSRunningApplication.runningApplications(withBundleIdentifier: bundle).filter {
                $0.processIdentifier != ProcessInfo.processInfo.processIdentifier && $0.activationPolicy != .prohibited
            }
            if !otherMenuApps.isEmpty { NSApp.terminate(nil); return }
        }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "keyboard", accessibilityDescription: "Orca LED")
        statusItem.button?.title = " LED"
        statusItem.button?.imagePosition = .imageLeading
        statusItem.button?.toolTip = "Orca LED · 상태와 설정"
        menu.delegate = self
        statusItem.menu = menu
        refreshMenu()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.updateStatusButton(self.connectionChecks(includeKeyboard: false))
        }
        DispatchQueue.main.async { [weak self] in self?.prepareAndStart() }
    }

    // Opening the app again while it already runs restarts a stopped bridge.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if bridgeProcess?.isRunning != true { prepareAndStart() }
        return false
    }

    func menuWillOpen(_ menu: NSMenu) { refreshMenu() }

    @discardableResult
    func item(_ title: String, action: Selector? = nil, enabled: Bool = true, into parent: NSMenu? = nil) -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
        entry.target = action == nil ? nil : self
        entry.isEnabled = enabled && action != nil
        (parent ?? menu).addItem(entry)
        return entry
    }

    func runtimeStatus() -> [String: Any]? {
        let url = runtime.appendingPathComponent("status.json")
        guard let metadata = try? url.resourceValues(forKeys: [.fileSizeKey]), (metadata.fileSize ?? 0) < 1024 * 1024,
              let data = try? Data(contentsOf: url),
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return value
    }

    // orca_led/orca_compat.json is the bridge's single list of live-verified
    // Orca versions. Other versions may run read-only (no F-key switching).
    func orcaVerified(_ version: String) -> Bool {
        let url = project.appendingPathComponent("orca_led/orca_compat.json")
        guard let data = try? Data(contentsOf: url),
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              value["schema"] as? Int == 1,
              let versions = value["verified_versions"] as? [String] else { return false }
        return versions.contains(version)
    }

    static func installedOrcaVersion() -> String? {
        Bundle(url: orcaApp)?.infoDictionary?["CFBundleShortVersionString"] as? String
    }

    static func compact(_ text: String, length: Int = 64) -> String {
        let cleaned = text.replacingOccurrences(of: "\n", with: " ").replacingOccurrences(of: "\r", with: " ")
        return cleaned.count > length ? String(cleaned.prefix(length - 1)) + "…" : cleaned
    }

    static func stateLabel(_ state: Int) -> String {
        ["미할당", "대기", "진행 중", "질문·승인 대기", "완료", "오류", "확인 불가", "완료 · 확인함"].indices.contains(state)
            ? ["미할당", "대기", "진행 중", "질문·승인 대기", "완료", "오류", "확인 불가", "완료 · 확인함"][state] : "확인 불가"
    }

    // MARK: Connection status

    enum Level: Int { case off = 0, ok, warn, error }

    struct Check {
        let name: String
        let detail: String
        let level: Level
        var action: Selector? = nil
    }

    static func reasonLabel(_ code: String) -> String {
        switch code {
        case "unsupported_version": return "Orca 버전을 확인할 수 없음"
        case "orca_layout_changed": return "Orca 화면 구조가 바뀌어 읽을 수 없음 · 업데이트 확인 필요"
        case "orca_bundle_unavailable": return "Orca 설치 파일을 읽을 수 없음"
        case "orca_restart_required": return "Orca 업데이트 적용 대기 · Orca 재시작 필요"
        case "runtime_unavailable", "runtime_closed": return "Orca 런타임에 연결할 수 없음"
        case "runtime_timeout": return "Orca 런타임 응답 시간 초과"
        case "runtime_access_denied": return "Orca 런타임 접근 거부"
        case "runtime_changed": return "Orca가 재시작됨 · 다시 연결 중"
        case "renderer_unavailable": return "Orca 화면이 아직 준비되지 않음"
        case "cdp_unavailable", "cdp_closed": return "개발 연결(19347)에 연결할 수 없음"
        case "cdp_timeout": return "개발 연결 응답 시간 초과"
        case "cdp_main_target_unavailable", "cdp_target_rejected": return "Orca 메인 화면을 찾지 못함"
        case "cdp_evaluation_failed": return "Orca 화면 구조가 달라 읽기 실패"
        case "stale_ui_snapshot", "ui_snapshot_unavailable": return "Orca 화면 정보가 갱신되지 않음"
        case "ui_order_unavailable": return "작업공간 순서를 읽을 수 없음"
        case "window_mismatch": return "Orca 창이 바뀜 · 다시 연결 중"
        case "remote_workspace_unsupported": return "원격 작업공간은 지원하지 않음"
        default: return "확인 실패 (\(code))"
        }
    }

    static func ago(_ seconds: TimeInterval) -> String {
        seconds < 1 ? "방금" : seconds < 60 ? "\(Int(seconds))초 전" : "\(Int(seconds / 60))분 전"
    }

    // Cheap checks only: this runs from the 2 s timer as well as on menu open.
    func connectionChecks(includeKeyboard: Bool) -> [Check] {
        var checks: [Check] = []
        let permissions = helper.permissions()
        let missing = [("손쉬운 사용", "accessibility"), ("입력 모니터링", "input_monitoring")]
            .filter { permissions[$0.1] as? Bool != true }.map { $0.0 }
        checks.append(missing.isEmpty ? Check(name: "macOS 권한", detail: "허용됨", level: .ok)
            : Check(name: "macOS 권한", detail: missing.joined(separator: "·") + " 필요", level: .error, action: #selector(requestPermissions)))

        let installed = Self.installedOrcaVersion()
        let orcaRunning = !Self.runningOrca().isEmpty
        let unverified = installed.map { orcaVerified($0) ? "" : " · 미검증" } ?? ""
        if installed == nil {
            checks.append(Check(name: "Orca", detail: "설치되지 않음", level: .error))
        } else if !orcaRunning {
            checks.append(Check(name: "Orca", detail: "실행 중 아님" + unverified, level: .off))
        } else if cdpEnabled && !Self.tcpPortOpen(Self.cdpPort) {
            checks.append(Check(name: "Orca", detail: "일반 모드 · 연동 모드로 다시 열어야 함", level: .warn))
        } else {
            checks.append(Check(name: "Orca", detail: "\(installed!)\(unverified) · \(cdpEnabled ? "연동 모드" : "실행 중")",
                                level: unverified.isEmpty ? .ok : .warn))
        }

        let running = bridgeProcess?.isRunning == true
        checks.append(preparingOrca ? Check(name: "연동 프로세스", detail: "Orca 준비 중…", level: .warn)
            : starting ? Check(name: "연동 프로세스", detail: "시작 중…", level: .warn)
            : stopping ? Check(name: "연동 프로세스", detail: "중지 중…", level: .warn)
            : running ? Check(name: "연동 프로세스", detail: "실행 중", level: .ok)
            : Check(name: "연동 프로세스", detail: "중지됨", level: .off))

        let status = runtimeStatus()
        let updated = status?["updated_at"] as? TimeInterval ?? 0
        let snapshot = status?["snapshot"] as? [String: Any]
        let now = Date().timeIntervalSince1970
        let current = running && updated >= startedAt
        let stale = current && now - updated >= 6
        if !running {
            checks.append(Check(name: "Orca 상태 읽기", detail: "연동 꺼짐", level: .off))
        } else if !current || snapshot == nil {
            checks.append(Check(name: "Orca 상태 읽기", detail: "첫 응답 대기 중…", level: .warn))
        } else if stale {
            checks.append(Check(name: "Orca 상태 읽기", detail: "응답 없음 · 마지막 갱신 \(Self.ago(now - updated))", level: .error))
        } else if snapshot?["supported"] as? Bool == true {
            let total = snapshot?["total_slots"] as? Int ?? 0
            if snapshot?["compatibility"] as? String == "verified" {
                checks.append(Check(name: "Orca 상태 읽기", detail: "정상 · 작업공간 \(total)개", level: .ok))
            } else {
                checks.append(Check(name: "Orca 상태 읽기", detail: "작업공간 \(total)개 · 미검증 버전이라 F키 이동 꺼짐", level: .warn))
            }
        } else {
            let reason = snapshot?["reason"] as? String ?? "unknown"
            checks.append(Check(name: "Orca 상태 읽기", detail: Self.reasonLabel(reason), level: .error))
        }

        let wireState = current && !stale ? status?["transport"] as? String : nil
        var keyboard: Check
        switch wireState {
        case "host_writes_accepted_unverified":
            keyboard = Check(name: "키보드", detail: "Bluetooth 전송 중 · 수신 확인 없음", level: .ok)
        case "disconnected_or_error":
            keyboard = Check(name: "키보드", detail: "전송 실패 · Bluetooth 연결 확인 필요", level: .error)
        case "ui_stale_lease_expiring":
            keyboard = Check(name: "키보드", detail: "Orca 상태가 없어 전송 보류", level: .warn)
        default:
            keyboard = Check(name: "키보드", detail: running ? "전송 대기 중…" : "연동 꺼짐", level: running ? .warn : .off)
        }
        if includeKeyboard && keyboard.level != .ok {
            // HID discovery is read-only but not free; only on menu open.
            let transports = ((try? helper.discover()) ?? []).map { transport($0) }
            if transports.isEmpty {
                keyboard = Check(name: "키보드", detail: "K10 Pro를 찾지 못함", level: running ? .error : .off)
            } else if !transports.contains(where: { $0.hasPrefix("Bluetooth") }) {
                keyboard = Check(name: "키보드", detail: "USB로만 연결됨 · Bluetooth 모드로 전환 필요", level: running ? .error : .warn)
            } else if !running {
                keyboard = Check(name: "키보드", detail: "Bluetooth 연결됨 · 연동 꺼짐", level: .off)
            }
        }
        checks.append(keyboard)
        return checks
    }

    static func dot(_ level: Level) -> NSImage? {
        let color: NSColor = [.off: .tertiaryLabelColor, .ok: .systemGreen, .warn: .systemOrange, .error: .systemRed][level]!
        return NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 9, weight: .regular)
                .applying(NSImage.SymbolConfiguration(paletteColors: [color])))
    }

    func updateStatusButton(_ checks: [Check]) {
        guard let button = statusItem?.button else { return }
        let worst = checks.map(\.level).max { $0.rawValue < $1.rawValue } ?? .off
        let running = bridgeProcess?.isRunning == true
        button.contentTintColor = worst == .error ? .systemRed : worst == .warn ? .systemOrange : nil
        button.appearsDisabled = !running && worst != .error
        let problems = checks.filter { $0.level == .error || $0.level == .warn }
        button.toolTip = "Orca LED · " + (problems.isEmpty ? (running ? "정상 연결" : "중지됨")
            : problems.map { "\($0.name): \($0.detail)" }.joined(separator: "\n"))
    }

    func refreshMenu() {
        menu.removeAllItems()
        menu.autoenablesItems = false
        let running = bridgeProcess?.isRunning == true
        let status = runtimeStatus()
        let updated = status?["updated_at"] as? TimeInterval ?? 0
        let snapshot = status?["snapshot"] as? [String: Any]
        let captured = snapshot?["captured_at"] as? TimeInterval ?? 0
        let now = Date().timeIntervalSince1970
        let fresh = running && updated >= startedAt && now - updated >= -2 && now - updated < 6
            && now - captured >= -2 && now - captured < 6
            && snapshot?["supported"] as? Bool == true && snapshot?["app_running"] as? Bool == true
            && status?["transport"] as? String == "host_writes_accepted_unverified"
        item("Orca LED · K10 Pro")
        item("연결 상태")
        let checks = connectionChecks(includeKeyboard: true)
        for check in checks {
            let row = item("\(check.name)  ·  \(check.detail)", action: check.action)
            row.image = Self.dot(check.level)
            row.indentationLevel = 1
            if check.action == nil { row.isEnabled = true }  // Keep text legible; it has no action.
        }
        updateStatusButton(checks)
        if let error = status?["error"] as? String, fresh, !error.isEmpty { item("주의: " + Self.compact(error)) }
        else if !running || preparingOrca { item(Self.compact(lastMessage)) }
        menu.addItem(.separator())
        item("펌웨어 검증 후 시작", action: #selector(startFromMenu), enabled: !running && !starting && !checkingFirmware && !preparingOrca)
        item("연동 중지", action: #selector(stopBridge), enabled: running && !stopping)

        let connectionMenu = NSMenu()
        let connectionItem = item("Orca 연결 · \(cdpEnabled ? "개발 연결 (19347)" : "파일 스냅샷")")
        connectionItem.isEnabled = true
        connectionItem.submenu = connectionMenu
        for (name, tag) in [("개발 연결 (19347)", 1), ("파일 스냅샷", 0)] {
            let option = item(name, action: #selector(setConnection(_:)), enabled: !starting && !stopping, into: connectionMenu)
            option.tag = tag
            option.state = (tag == 1) == cdpEnabled ? .on : .off
        }
        item("시작 시 Orca를 개발 연결 모드로 엽니다.", into: connectionMenu)
        item("일반 모드로 실행 중이면 확인 후 다시 엽니다.", into: connectionMenu)

        let brightnessMenu = NSMenu()
        let brightnessItem = item("LED 밝기 · \(Int(round(Double(brightness) / 255 * 100)))%")
        brightnessItem.isEnabled = true
        brightnessItem.submenu = brightnessMenu
        for (name, value) in [("꺼짐", 0), ("은은하게 · 28%", 72), ("50%", 128), ("75%", 192), ("100%", 255)] {
            let option = item(name, action: #selector(setBrightness(_:)), enabled: !starting && !stopping, into: brightnessMenu)
            option.tag = value
            option.state = value == brightness ? .on : .off
        }
        item("실행 중 변경하면 연동을 다시 시작해 적용합니다.", into: brightnessMenu)
        menu.addItem(.separator())
        item(fresh ? "F1–F12 · 현재 대상" : "F1–F12 · 마지막 기록 / 현재 상태 미확인")
        let rows = snapshot?["slots"] as? [[String: Any]] ?? []
        for index in 1...12 {
            let row = rows.first { $0["index"] as? Int == index }
            let name = (row?["label"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "미할당"
            let value = fresh ? (row?["state"] as? Int ?? 0) : (row == nil ? 0 : 6)
            item("F\(index)  \(Self.compact(name, length: 28))  · \(Self.stateLabel(value))")
        }
        menu.addItem(.separator())
        item("색상과 효과 설정…", action: #selector(openSettings))
        item("연결·권한 확인…", action: #selector(showDiagnostics))
        item("USB 펌웨어 확인…", action: #selector(checkFirmware), enabled: !running && !starting && !checkingFirmware)
        item("필요한 macOS 권한 요청…", action: #selector(requestPermissions))
        item(bundled ? "데이터 폴더 열기" : "프로젝트 폴더 열기", action: #selector(openProject))
        item("실행 기록 열기", action: #selector(openLog))
        menu.addItem(.separator())
        item("Orca LED 종료", action: #selector(quit))
    }

    func environment() -> [String: String] {
        var result = ProcessInfo.processInfo.environment
        result["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        result["PYTHONPATH"] = project.path
        result["PYTHONUNBUFFERED"] = "1"
        // Never write bytecode caches into the signed app bundle.
        result["PYTHONDONTWRITEBYTECODE"] = "1"
        return result
    }

    static func findPython() -> String? {
        for path in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"] {
            guard FileManager.default.isExecutableFile(atPath: path) else { continue }
            let process = Process()
            process.executableURL = URL(fileURLWithPath: path)
            process.arguments = ["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            do { try process.run() } catch { continue }
            let deadline = Date().addingTimeInterval(2)
            while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.02) }
            if process.isRunning { process.terminate(); continue }
            if process.terminationStatus == 0 { return path }
        }
        return nil
    }

    // MARK: Orca local debugging launch

    static func runningOrca() -> [NSRunningApplication] {
        NSRunningApplication.runningApplications(withBundleIdentifier: orcaBundleID).filter {
            !$0.isTerminated && $0.bundleURL?.standardizedFileURL.path == orcaApp.path
        }
    }

    static func tcpPortOpen(_ port: Int) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(UInt16(port).bigEndian)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        return withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0 }
        }
    }

    static func unixSocketAnswers(_ path: String) -> Bool {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: address.sun_path) else { return false }
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: bytes) }
        return withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) == 0 }
        }
    }

    // The renderer is ready once the debugging endpoint lists a page target.
    static func cdpPageReady() -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(cdpPort)/json/list") else { return false }
        let request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 1.5)
        let done = DispatchSemaphore(value: 0)
        let result = NSLock()
        var ready = false
        URLSession.shared.dataTask(with: request) { data, response, _ in
            if (response as? HTTPURLResponse)?.statusCode == 200, let data,
               let list = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] {
                result.lock(); ready = list.contains { $0["type"] as? String == "page" }; result.unlock()
            }
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 2)
        result.lock(); defer { result.unlock() }
        return ready
    }

    // Orca rewrites its runtime metadata on launch; require a fresh, answering socket.
    static func runtimeReady(since: Date?) -> Bool {
        let base = ProcessInfo.processInfo.environment["ORCA_USER_DATA_PATH"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/orca")
        let url = base.appendingPathComponent("orca-runtime.json")
        if let since, let modified = try? url.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate,
           modified < since.addingTimeInterval(-2) { return false }
        guard let data = try? Data(contentsOf: url),
              let meta = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        let transports = (meta["transports"] as? [[String: Any]]) ?? [meta["transport"] as? [String: Any]].compactMap { $0 }
        guard let endpoint = transports.first(where: { $0["kind"] as? String == "unix" })?["endpoint"] as? String else { return false }
        return unixSocketAnswers(endpoint)
    }

    @objc func startFromMenu() { prepareAndStart() }

    func prepareAndStart() {
        guard bridgeProcess?.isRunning != true, !starting, !stopping, !checkingFirmware, !preparingOrca, !quitting else { return }
        guard cdpEnabled else { startBridge(); return }
        guard Self.installedOrcaVersion() != nil else {
            alert("Orca를 찾지 못했습니다.", "지원하는 설치 경로: /Applications/Orca.app")
            return
        }
        preparingOrca = true
        lastMessage = "Orca 연동 모드를 확인하는 중…"
        refreshMenu()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let running = !Self.runningOrca().isEmpty
            let portOpen = Self.tcpPortOpen(Self.cdpPort)
            DispatchQueue.main.async {
                guard let self, !self.quitting else { return }
                switch (running, portOpen) {
                case (true, true): self.waitForOrca(since: nil)
                case (true, false): self.confirmOrcaRestart()
                case (false, true):
                    self.finishPreparing("로컬 연결 포트 \(Self.cdpPort)를 다른 프로그램이 사용 중입니다.")
                    self.alert("Orca를 연동 모드로 열 수 없습니다.", self.lastMessage)
                case (false, false): self.launchOrca()
                }
            }
        }
    }

    func finishPreparing(_ message: String) {
        preparingOrca = false
        lastMessage = message
        refreshMenu()
    }

    func confirmOrcaRestart() {
        let alert = NSAlert()
        alert.messageText = "Orca를 연동 모드로 다시 열까요?"
        var detail = "Orca가 일반 모드로 실행 중이라 LED 연동을 연결할 수 없습니다.\n\nOrca를 종료(Cmd+Q와 같은 방식)한 뒤 연동 모드로 다시 엽니다. 저장하지 않은 작업이 있으면 먼저 정리해 주세요. Orca가 종료 확인을 물으면 직접 답해 주세요."
        if let installed = Self.installedOrcaVersion(), !orcaVerified(installed) {
            detail += "\n\nOrca \(installed)은(는) 아직 검증되지 않은 버전입니다. LED 상태 표시는 시도하지만 F키 작업공간 이동은 꺼집니다."
        }
        alert.informativeText = detail
        alert.addButton(withTitle: "다시 열기")
        alert.addButton(withTitle: "취소")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else {
            finishPreparing("Orca가 일반 모드로 실행 중이라 연동을 시작하지 않았습니다.")
            return
        }
        lastMessage = "Orca 종료를 기다리는 중…"
        refreshMenu()
        Self.runningOrca().forEach { $0.terminate() }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            // Leave time for Orca's own quit confirmation; never force-quit.
            let deadline = Date().addingTimeInterval(90)
            var closed = false
            while Date() < deadline {
                if Self.runningOrca().isEmpty && !Self.tcpPortOpen(Self.cdpPort) { closed = true; break }
                Thread.sleep(forTimeInterval: 0.5)
            }
            DispatchQueue.main.async {
                guard let self, !self.quitting else { return }
                if closed { self.launchOrca() }
                else { self.finishPreparing("Orca가 종료되지 않아 연동을 시작하지 않았습니다. Orca를 종료한 뒤 다시 시도해 주세요.") }
            }
        }
    }

    func launchOrca() {
        lastMessage = "Orca를 연동 모드로 여는 중…"
        refreshMenu()
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.arguments = ["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=\(Self.cdpPort)"]
        configuration.activates = true
        let since = Date()
        NSWorkspace.shared.openApplication(at: Self.orcaApp, configuration: configuration) { [weak self] _, error in
            DispatchQueue.main.async {
                guard let self, !self.quitting else { return }
                if let error {
                    self.finishPreparing("Orca 실행 실패: " + error.localizedDescription)
                    self.alert("Orca를 열지 못했습니다.", self.lastMessage)
                } else { self.waitForOrca(since: since) }
            }
        }
    }

    func waitForOrca(since: Date?) {
        lastMessage = "Orca가 준비되기를 기다리는 중…"
        refreshMenu()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let deadline = Date().addingTimeInterval(60)
            var ready = false
            while Date() < deadline {
                if Self.cdpPageReady() && Self.runtimeReady(since: since) { ready = true; break }
                Thread.sleep(forTimeInterval: 0.5)
            }
            // Give the freshly loaded renderer a moment to finish its own startup.
            if ready && since != nil { Thread.sleep(forTimeInterval: 3) }
            DispatchQueue.main.async {
                guard let self, !self.quitting else { return }
                guard ready else {
                    self.finishPreparing("Orca 연동 모드 응답을 60초 안에 확인하지 못했습니다.")
                    self.alert("Orca 연결을 확인하지 못했습니다.", self.lastMessage)
                    return
                }
                self.preparingOrca = false
                self.autoRetries = 2
                self.startBridge()
            }
        }
    }

    @objc func startBridge() {
        guard bridgeProcess?.isRunning != true, !starting, !checkingFirmware, !quitting else { return }
        guard FileManager.default.fileExists(atPath: project.appendingPathComponent("orca_led/__main__.py").path) else {
            alert("연동 코드를 찾지 못했습니다.", bundled ? "앱 파일이 손상되었을 수 있습니다. Orca LED.app을 다시 내려받아 설치해 주세요." : "Orca LED.app을 프로젝트 폴더 안에 두고 다시 실행해 주세요.")
            return
        }
        starting = true
        refreshMenu()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let python = Self.findPython()
            DispatchQueue.main.async {
                guard let self else { return }
                self.starting = false
                guard !self.quitting else { NSApp.reply(toApplicationShouldTerminate: true); return }
                guard let python else {
                    self.lastMessage = "Python 3.9 이상이 필요합니다. 터미널에서 xcode-select --install 로 Command Line Tools를 설치해 주세요."
                    self.alert("실행 환경을 찾지 못했습니다.", self.lastMessage)
                    self.refreshMenu()
                    return
                }
                self.launchBridge(python)
            }
        }
    }

    func launchBridge(_ python: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.currentDirectoryURL = dataRoot
        process.environment = environment()
        var arguments = ["-m", "orca_led", "--helper", Bundle.main.executableURL!.path,
                             "run", "--firmware-installed", "--brightness", String(brightness),
                             "--status-file", runtime.appendingPathComponent("status.json").path]
        if cdpEnabled { arguments += ["--cdp-port", "19347"] }
        process.arguments = arguments
        do {
            try FileManager.default.createDirectory(at: runtime, withIntermediateDirectories: true)
            let logURL = runtime.appendingPathComponent("menu-bridge.log")
            if !FileManager.default.fileExists(atPath: logURL.path) { FileManager.default.createFile(atPath: logURL.path, contents: nil) }
            let log = try FileHandle(forWritingTo: logURL)
            try log.seekToEnd()
            log.write(Data("\n--- Orca LED 시작 \(ISO8601DateFormatter().string(from: Date())) ---\n".utf8))
            bridgeLog = log
            process.standardOutput = log
            process.standardError = log
            process.standardInput = FileHandle.nullDevice
            process.terminationHandler = { [weak self] child in
                DispatchQueue.main.async { self?.bridgeExited(child) }
            }
            bridgeProcess = process
            startedAt = Date().timeIntervalSince1970
            try process.run()
            lastMessage = "시작을 요청했습니다. 실제 연결 상태를 확인하는 중입니다."
        } catch {
            bridgeProcess = nil
            try? bridgeLog?.close(); bridgeLog = nil
            lastMessage = "시작 실패: " + error.localizedDescription
            alert("연동을 시작하지 못했습니다.", lastMessage)
        }
        refreshMenu()
    }

    func bridgeExited(_ child: Process) {
        guard bridgeProcess === child else { return }
        let intentionallyStopped = stopping
        bridgeProcess = nil
        stopping = false
        try? bridgeLog?.close(); bridgeLog = nil
        lastMessage = intentionallyStopped ? "연동을 중지했습니다. 키보드는 기본 동작으로 돌아갑니다." : "연동이 종료되었습니다. 실행 기록을 확인해 주세요."
        if !intentionallyStopped && child.terminationStatus != 0 { lastMessage = "실행 실패 · 실행 기록에서 원인을 확인해 주세요." }
        if quitting { NSApp.reply(toApplicationShouldTerminate: true); return }
        // An early failure right after Orca launches is retried a couple of times.
        if !intentionallyStopped && child.terminationStatus != 0 && autoRetries > 0
            && Date().timeIntervalSince1970 - startedAt < 30 {
            autoRetries -= 1
            lastMessage = "Orca 연결을 다시 시도하는 중…"
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in self?.startBridge() }
            refreshMenu()
            return
        }
        autoRetries = 0
        if restartAfterStop {
            restartAfterStop = false
            startBridge()
        }
        refreshMenu()
    }

    @objc func stopBridge() {
        guard let process = bridgeProcess, process.isRunning else { return }
        stopping = true
        process.terminate()
        refreshMenu()
    }

    @objc func setBrightness(_ sender: NSMenuItem) {
        brightness = sender.tag
        saveSettings()
        if bridgeProcess?.isRunning == true { restartAfterStop = true; stopBridge() }
        refreshMenu()
    }

    @objc func setConnection(_ sender: NSMenuItem) {
        cdpEnabled = sender.tag == 1
        saveSettings()
        if bridgeProcess?.isRunning == true { restartAfterStop = true; stopBridge() }
        refreshMenu()
    }

    func saveSettings() {
        do {
            try FileManager.default.createDirectory(at: runtime, withIntermediateDirectories: true)
            try JSONSerialization.data(withJSONObject: ["brightness": brightness, "cdp_enabled": cdpEnabled]).write(to: runtime.appendingPathComponent("menu-settings.json"), options: .atomic)
        } catch { lastMessage = "설정 저장 실패: " + error.localizedDescription }
    }

    @objc func showDiagnostics() {
        let permissions = helper.permissions()
        let session = helper.sessionStatus()
        let devices = (try? helper.discover().map(helper.describe)) ?? []
        let keyboard = devices.isEmpty ? "연결된 K10 Pro를 찾지 못함" : devices.map { "K10 Pro · \($0["transport"] ?? "연결 방식 미확인")" }.joined(separator: "\n")
        let access = permissions["accessibility"] as? Bool == true ? "허용됨" : "설정 필요"
        let input = permissions["input_monitoring"] as? Bool == true ? "허용됨" : "설정 필요"
        let currentSession = session["active"] as? Bool == true ? "로컬 화면 사용 가능" : "잠금 또는 화면 상태 확인 필요"
        let orcaHint = cdpEnabled ? "Orca 개발 연결: 포트 19347\nOrca LED를 실행하거나 ‘펌웨어 검증 후 시작’을 누르면 Orca를 개발 연결 모드로 엽니다." : "Orca 파일 스냅샷: runtime/orca-ui.json\n현재 화면 정보가 계속 갱신되어야 합니다."
        alert("연결·권한 확인", "\(keyboard)\n\n손쉬운 사용: \(access)\n입력 모니터링: \(input)\n\(currentSession)\n\n\(orcaHint)\n\n키보드 발견만으로 펌웨어나 LED 수신이 검증된 것은 아닙니다.")
    }

    @objc func checkFirmware() {
        guard bridgeProcess?.isRunning != true, !checkingFirmware else { return }
        checkingFirmware = true
        refreshMenu()
        let base = dataRoot
        try? FileManager.default.createDirectory(at: runtime, withIntermediateDirectories: true)
        let executable = Bundle.main.executableURL!.path
        let env = environment()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var message = "Python 3.9 이상을 찾지 못했습니다. 터미널에서 xcode-select --install 로 Command Line Tools를 설치해 주세요."
            var verified = false
            if let python = Self.findPython() {
                let process = Process()
                let output = Pipe()
                process.executableURL = URL(fileURLWithPath: python)
                process.currentDirectoryURL = base
                process.environment = env
                process.arguments = ["-m", "orca_led", "--helper", executable, "verify-usb"]
                process.standardOutput = output
                process.standardError = output
                process.standardInput = FileHandle.nullDevice
                do {
                    try process.run()
                    let deadline = Date().addingTimeInterval(8)
                    while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.03) }
                    if process.isRunning { process.terminate(); message = "USB 확인 시간이 초과되었습니다." }
                    else {
                        let data = output.fileHandleForReading.readDataToEndOfFile()
                        if process.terminationStatus == 0,
                           let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any], result["firmware_verified"] as? Bool == true {
                            verified = true
                            message = "연결된 키보드에서 Orca LED 펌웨어 v1 응답을 받았습니다.\n\n키보드를 Bluetooth 모드로 바꾼 뒤 ‘펌웨어 검증 후 시작’을 선택하세요."
                        } else { message = String(data: data, encoding: .utf8).map { String($0.suffix(1800)) } ?? "키보드의 펌웨어 응답을 확인하지 못했습니다." }
                    }
                } catch { message = error.localizedDescription }
            }
            DispatchQueue.main.async {
                guard let self else { return }
                self.checkingFirmware = false
                self.alert(verified ? "USB 펌웨어 확인 완료" : "USB 펌웨어 확인 필요", message)
                self.refreshMenu()
            }
        }
    }

    @objc func requestPermissions() {
        _ = try? helper.dispatch(["op": "request-permissions", "accessibility": true, "input_monitoring": true])
        alert("macOS 권한 설정", "시스템 설정 → 개인정보 보호 및 보안에서 ‘손쉬운 사용’과 ‘입력 모니터링’의 Orca LED를 켜 주세요.\n\n권한을 설정한 뒤 이 메뉴의 ‘연결·권한 확인’에서 확인할 수 있습니다.")
    }

    func alert(_ title: String, _ message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "확인")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    @objc func openSettings() {
        let candidates = [Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/Orca LED Settings.app"),
                          project.appendingPathComponent("Orca LED Settings.app"), project.appendingPathComponent("build/Orca LED Settings.app")]
        if let app = candidates.first(where: { FileManager.default.fileExists(atPath: $0.path) }) {
            NSWorkspace.shared.openApplication(at: app, configuration: NSWorkspace.OpenConfiguration(), completionHandler: nil)
        } else { alert("설정 앱을 찾지 못했습니다.", "Orca LED.app을 다시 내려받아 설치해 주세요.") }
    }
    @objc func openProject() {
        try? FileManager.default.createDirectory(at: dataRoot, withIntermediateDirectories: true)
        NSWorkspace.shared.open(dataRoot)
    }
    @objc func openLog() {
        let log = runtime.appendingPathComponent("menu-bridge.log")
        if FileManager.default.fileExists(atPath: log.path) { NSWorkspace.shared.open(log) }
        else { alert("실행 기록 없음", "이 앱에서 연동을 시작하면 실행 기록이 저장됩니다.") }
    }
    @objc func quit() { NSApp.terminate(nil) }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        quitting = true
        restartAfterStop = false
        if starting { return .terminateLater }
        if bridgeProcess?.isRunning == true { stopBridge(); return .terminateLater }
        return .terminateNow
    }

    func applicationWillTerminate(_ notification: Notification) { timer?.invalidate(); helper.shutdown() }
}

func launchStatusApp(helper: Helper) {
    let application = NSApplication.shared
    application.setActivationPolicy(.accessory)
    let delegate = StatusAppDelegate(helper: helper)
    application.delegate = delegate
    withExtendedLifetime(delegate) { application.run() }
}
