import Foundation
import Observation

struct PreviewSlot: Identifiable {
    let id: Int
    let state: Int
    let label: String
}

struct ThemeFirmwareReceipt: Decodable {
    let schema: Int
    let theme_protocol: Int
    let verified_via: String
    let verified_at: Double
    let vendor_id: Int
    let product_id: Int
    var valid: Bool {
        schema == 1 && (1...2).contains(theme_protocol) && verified_via == "usb_query" && vendor_id == 13364 && product_id == 672
            && verified_at.isFinite && verified_at > 0 && verified_at <= Date().timeIntervalSince1970
    }
    static func decode(_ data: Data) throws -> ThemeFirmwareReceipt {
        try rejectDuplicateJSONKeys(data)
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(root.keys) == ["schema", "theme_protocol", "verified_via", "verified_at", "vendor_id", "product_id"],
              ["schema", "theme_protocol", "vendor_id", "product_id"].allSatisfy({ isJSONInteger(root[$0]) }) else {
            throw ThemeError.invalid("펌웨어 확인 기록이 올바르지 않습니다.")
        }
        return try JSONDecoder().decode(ThemeFirmwareReceipt.self, from: data)
    }
}

@MainActor @Observable final class ThemeStore {
    var draft: LEDTheme
    var saved: LEDTheme
    var selectedIndex = 1
    var editGeneration = 0
    var previewMode = PreviewMode.sample
    var liveSlots: [PreviewSlot] = []
    var liveFresh = false
    var phase = "legacy"
    var sentCount = 0
    var total = 6
    var error: String?
    var notice: String?
    var loadedFromDisk = false
    var statusRevision: String?
    var firmwareReady = false
    var firmwareVerifiedAt: Date?
    var loadingFiles = false
    var saving = false
    var fileAccess = FileAccess.pending
    var initialLoadFinished = false
    var externalChange = false
    let demoOnly: Bool
    let project: URL
    let runtime: URL
    private var timer: Timer?
    private var preserveInvalidOriginal = false
    private let fileIO: ThemeFileIO
    private var observedData: Data?
    private var lastStatusTime: TimeInterval = 0
    private var retryAfter = Date.distantPast

    enum FileAccess { case pending, ready, denied, failed }

    enum PreviewMode: String, CaseIterable, Identifiable {
        case live = "실제 작업", sample = "모든 상태"
        var id: String { rawValue }
    }

    init(project: URL, demoOnly: Bool = false, testReadDelay: TimeInterval = 0) {
        self.project = project
        self.demoOnly = demoOnly
        runtime = project.appendingPathComponent("runtime", isDirectory: true)
        fileIO = ThemeFileIO(runtime: runtime, testDelay: testReadDelay)
        let defaults = LEDTheme.defaults
        draft = defaults
        saved = defaults
        // No filesystem calls here. The window must be visible before macOS
        // requests Desktop access, which can leave a file open pending forever.
    }
    var dirty: Bool { !draft.sameSettings(as: saved) }
    var selected: LEDStateTheme { draft.states[selectedIndex] }
    var slots: [PreviewSlot] {
        if previewMode == .sample {
            let states = [2, 3, 4, 7, 5, 1, 6, 2, 3, 4, 7, 0]
            return states.enumerated().map { PreviewSlot(id: $0.offset + 1, state: $0.element, label: LEDStateTheme.labels[$0.element] ?? "") }
        }
        return (1...12).map { id in liveSlots.first { $0.id == id } ?? PreviewSlot(id: id, state: 0, label: "미할당") }
    }
    func select(state: Int) {
        guard let index = draft.states.firstIndex(where: { $0.state == state }) else { return }
        selectedIndex = index
    }
    func restoreDefaults() {
        draft = LEDTheme.defaults
        editGeneration += 1
        notice = "기본값을 미리 보고 있습니다. 저장하면 적용됩니다."
        error = nil
    }
    func discard() {
        draft = saved
        editGeneration += 1
        notice = "저장 전 변경 사항을 취소했습니다."
        error = nil
    }
    var canSave: Bool { !demoOnly && initialLoadFinished && fileAccess == .ready && !saving && !loadingFiles && !externalChange }
    @discardableResult func save() async -> Bool {
        guard canSave else { return false }
        saving = true
        defer { saving = false }
        do {
            var next = try draft.validated()
            next.revision = UUID().uuidString.lowercased()
            let data = try next.encoded()
            let submitted = draft
            try await fileIO.save(data, expected: observedData, preserveInvalid: preserveInvalidOriginal)
            observedData = data
            preserveInvalidOriginal = false
            saved = next
            if draft.sameSettings(as: submitted) { draft = next }
            loadedFromDisk = true
            notice = firmwareReady ? "저장했습니다. 연동 중이면 키보드로 전송을 요청합니다." : "저장했습니다. 펌웨어 업데이트 후 키보드에 적용할 수 있습니다."
            error = nil
            return true
        } catch { self.error = "저장하지 못했습니다. \(error.localizedDescription)"; return false }
    }
    func startPolling() {
        guard timer == nil, !demoOnly else { return }
        Task { await refreshStatus() }
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if Date().timeIntervalSince1970 - self.lastStatusTime >= 6 { self.liveFresh = false }
                if Date() >= self.retryAfter { await self.refreshStatus() }
            }
        }
    }
    func stopPolling() { timer?.invalidate(); timer = nil }

    func refreshStatus(discardEdits: Bool = false) async {
        guard !demoOnly, !loadingFiles, !saving else { return }
        loadingFiles = true
        defer { loadingFiles = false }
        let result = await fileIO.read()
        apply(result, discardEdits: discardEdits)
    }
    func apply(_ result: ThemeFilesSnapshot, discardEdits: Bool = false) {
        if result.denied {
            fileAccess = .denied; liveFresh = false
            retryAfter = Date().addingTimeInterval(5)
            return
        }
        guard result.theme.readable else {
            fileAccess = .failed; liveFresh = false
            retryAfter = Date().addingTimeInterval(5)
            return
        }
        fileAccess = .ready
        let firstLoad = !initialLoadFinished
        let changed = result.theme.data != observedData
        if firstLoad || changed || discardEdits {
            if !firstLoad && dirty && changed && !discardEdits {
                externalChange = true
                notice = "다른 곳에서 설정이 변경되었습니다. 다시 불러오면 현재 미리보기는 취소됩니다."
            } else {
                let preserveDraft = firstLoad && dirty && !discardEdits
                observedData = result.theme.data
                externalChange = false
                if let data = result.theme.data {
                    do {
                        let theme = try LEDTheme.decode(data)
                        saved = theme
                        if !preserveDraft { draft = theme; editGeneration += 1 }
                        loadedFromDisk = true
                        preserveInvalidOriginal = false
                        error = nil
                    } catch {
                        preserveInvalidOriginal = true
                        self.error = "저장된 설정을 읽지 못했습니다. 원본은 보존되어 있습니다. \(error.localizedDescription)"
                    }
                } else {
                    loadedFromDisk = false
                    preserveInvalidOriginal = false
                    if !preserveDraft { saved = LEDTheme.defaults; draft = saved; editGeneration += 1 }
                }
                if preserveDraft { notice = "저장된 설정을 불러왔습니다. 편집 중인 미리보기는 유지했습니다. '변경 취소'로 저장된 설정을 볼 수 있어요." }
            }
        }
        initialLoadFinished = true
        if let data = result.firmware.data,
           let receipt = try? ThemeFirmwareReceipt.decode(data), receipt.valid {
            firmwareReady = true
            firmwareVerifiedAt = Date(timeIntervalSince1970: receipt.verified_at)
        } else { firmwareReady = false; firmwareVerifiedAt = nil }
        guard let statusData = result.status.data,
              let status = try? JSONSerialization.jsonObject(with: statusData) as? [String: Any] else {
            liveFresh = false
            phase = "legacy"
            return
        }
        let updated = status["updated_at"] as? Double ?? 0
        let age = Date().timeIntervalSince1970 - updated
        let snapshot = status["snapshot"] as? [String: Any]
        let captured = snapshot?["captured_at"] as? Double ?? 0
        lastStatusTime = min(updated, captured)
        let captureAge = Date().timeIntervalSince1970 - captured
        let transport = status["transport"] as? String ?? ""
        liveFresh = age >= -2 && age < 6 && captureAge >= -2 && captureAge < 6
            && snapshot?["supported"] as? Bool == true && snapshot?["app_running"] as? Bool == true
            && transport == "host_writes_accepted_unverified"
        let slots = snapshot?["slots"] as? [[String: Any]] ?? []
        liveSlots = slots.compactMap { row in
            guard let index = row["index"] as? Int, (1...12).contains(index) else { return nil }
            let state = row["state"] as? Int ?? 6
            return PreviewSlot(id: index, state: (0...7).contains(state) ? state : 6, label: row["label"] as? String ?? "")
        }
        if let theme = status["theme"] as? [String: Any] {
            phase = theme["phase"] as? String ?? "legacy"
            statusRevision = theme["revision"] as? String
            sentCount = max(0, theme["sent_count"] as? Int ?? 0)
            total = max(0, theme["total"] as? Int ?? 6)
        } else { phase = "legacy"; statusRevision = nil }
        if firstLoad && liveFresh { previewMode = .live }
    }
    var deliveryTitle: String {
        if demoOnly { return "화면 미리보기 · 키보드 설정은 변경하지 않습니다" }
        if saving { return "설정 저장 중…" }
        if fileAccess != .ready { return "예시 상태 미리보기 · 저장 폴더 연결 대기" }
        if externalChange { return "외부 변경 확인 필요" }
        if dirty { return "저장 전 미리보기" }
        if !loadedFromDisk { return "기본 설정" }
        guard firmwareReady else { return "저장됨 · 펌웨어 업데이트 대기" }
        guard liveFresh else { return "저장됨 · 연동 시작 대기" }
        guard statusRevision == saved.revision else { return "저장됨 · 적용 대기" }
        switch phase {
        case "sending": return "전송 요청 중 · \(min(sentCount, total))/\(total)"
        case "sent": return "전송 요청 완료 · 실제 조명을 확인해 주세요"
        case "error": return "전송 오류 · 설정은 저장되어 있습니다"
        case "pending": return "저장됨 · 적용 대기"
        default: return "저장됨 · 펌웨어 업데이트 대기"
        }
    }
    var accessNoticeTitle: String? {
        guard !demoOnly else { return nil }
        if externalChange { return "다른 곳에서 설정이 변경되었습니다." }
        switch fileAccess {
        case .pending: return "설정 폴더 접근을 기다리고 있어요."
        case .denied: return "설정을 읽고 저장하려면 폴더 접근이 필요해요."
        case .failed: return "저장된 설정을 읽지 못했어요."
        case .ready: return nil
        }
    }
    var accessNoticeDetail: String? {
        guard !demoOnly else { return nil }
        if externalChange { return "다시 불러오면 현재 미리보기를 취소하고 저장된 설정을 엽니다." }
        switch fileAccess {
        case .pending: return "macOS의 폴더 접근 안내에서 허용해 주세요. 기다리는 동안 예시 색상을 살펴볼 수 있어요."
        case .denied: return "시스템 설정 → 개인정보 보호 및 보안 → 파일 및 폴더에서 Orca LED Settings를 허용해 주세요."
        case .failed: return "폴더 접근과 설정 파일을 확인해 주세요. 기존 파일은 변경하지 않았습니다."
        case .ready: return nil
        }
    }
}
