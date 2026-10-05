import Foundation

@MainActor enum SettingsSelfTests {
    static func run() async throws {
        var count = 0
        func check(_ condition: @autoclosure () -> Bool, _ name: String) throws {
            guard condition() else { throw ThemeError.invalid(name) }
            count += 1
        }
        func rejected(_ theme: LEDTheme, _ name: String) throws {
            do { _ = try theme.validated() } catch { count += 1; return }
            throw ThemeError.invalid(name)
        }
        let original = LEDTheme.defaults
        let decoded = try JSONDecoder().decode(LEDTheme.self, from: original.encoded()).validated()
        try check(decoded == original, "JSON round trip")
        try check(original.state(0) == nil, "unassigned state is not configurable")
        try check(LEDStateTheme.canonicalHex("aAbBcC") == "#AABBCC", "hex canonicalization")
        try check(LEDStateTheme.canonicalHex("#GG0000") == nil, "reject nonhex")
        try check(LEDStateTheme.canonicalHex("ＦＦ0000") == nil, "reject Unicode digits")
        try check(LEDStateTheme.canonicalHex("#0000000") == nil, "reject oversized color")
        var invalid = original
        invalid.global_brightness = 256
        try rejected(invalid, "brightness range")
        invalid = original; invalid.states[0].brightness = -1
        try rejected(invalid, "state brightness range")
        invalid = original; invalid.states[0].period_ms = 801
        try rejected(invalid, "period step")
        invalid = original; invalid.states[0].period_ms = 10001
        try rejected(invalid, "period limit")
        invalid = original; invalid.states[0].color = "aabbcc"
        try rejected(invalid, "stored color requires hash")
        invalid = original; invalid.states.append(original.states[0])
        try rejected(invalid, "extra state rejected")
        invalid = original; invalid.revision = "not-a-uuid"
        try rejected(invalid, "revision UUID")
        invalid = original; invalid.schema = 2
        try rejected(invalid, "unknown schema")
        var legacy = original
        legacy.states.removeAll { $0.state == 7 }
        legacy.states[legacy.states.firstIndex { $0.state == 4 }!].color = "#123456"
        let upgraded = try legacy.validated()
        try check(upgraded.states.map(\.state) == LEDStateTheme.displayOrder, "six-state theme gains acknowledged completion")
        try check(upgraded.state(7)?.color == "#123456" && upgraded.state(7)?.effect == .solid, "acknowledged completion keeps old completion color")
        legacy.states.removeLast()
        try rejected(legacy, "missing state rejected")
        try check(LEDEffect.off.amplitude(at: 0, period: 1000, reduceMotion: true) == 0, "off remains off")
        try check(LEDEffect.breathe.amplitude(at: 0.5, period: 1000, reduceMotion: false) == 1, "breathe peak")
        try check(LEDEffect.breathe.amplitude(at: 0, period: 1000, reduceMotion: false) == 55.0 / 255, "breathe floor")
        try check(LEDEffect.double.amplitude(at: 0.15, period: 1000, reduceMotion: false) == 0, "double gap")
        try check(LEDEffect.double.amplitude(at: 0.25, period: 1000, reduceMotion: false) == 1, "double second pulse")
        try check(LEDEffect.triple.amplitude(at: 0.45, period: 1000, reduceMotion: false) == 1, "triple third pulse")
        try check(LEDEffect.blink.amplitude(at: 0.75, period: 1000, reduceMotion: true) == 1, "reduce motion steady")
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("orca-led-settings-test-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = ThemeStore(project: folder)
        try check(!FileManager.default.fileExists(atPath: folder.path), "opening does not write")
        store.draft.states[1].color = "#AABBCC"
        try check(store.dirty, "draft tracks edits")
        store.discard()
        try check(!store.dirty && store.draft.states[1].color == "#0033FF", "discard restores")
        try check(store.editGeneration == 1, "discard resets focused editor state")
        store.draft.states[1].color = "#AABBCC"
        await store.refreshStatus()
        await store.save()
        try check(store.error == nil && !store.dirty, "save success")
        let savedRevision = store.saved.revision
        let loaded = ThemeStore(project: folder)
        await loaded.refreshStatus()
        try check(loaded.saved.states[1].color == "#AABBCC" && loaded.loadedFromDisk, "saved data reloaded")
        await loaded.save()
        try check(loaded.saved.revision != savedRevision, "retry creates fresh revision")
        let beforeDemo = try Data(contentsOf: folder.appendingPathComponent("runtime/theme.json"))
        let demo = ThemeStore(project: folder, demoOnly: true)
        demo.draft.states[0].brightness = 0; await demo.save()
        let afterDemo = try Data(contentsOf: folder.appendingPathComponent("runtime/theme.json"))
        try check(beforeDemo == afterDemo, "demo cannot write")
        try check(demo.slots.count == 12 && demo.slots.last?.state == 0, "demo fixture")
        let now = Date().timeIntervalSince1970
        let runtime = folder.appendingPathComponent("runtime")
        let lowercase = String(decoding: beforeDemo, as: UTF8.self).replacingOccurrences(of: "#AABBCC", with: "#aabbcc")
        try Data(lowercase.utf8).write(to: runtime.appendingPathComponent("theme.json"))
        let normalized = ThemeStore(project: folder)
        await normalized.refreshStatus()
        try check(normalized.loadedFromDisk && normalized.saved.states[1].color == "#AABBCC", "lowercase saved theme normalized")
        let duplicate = Data("{\"schema\":1,\"sch\\u0065ma\":1}".utf8)
        do { _ = try LEDTheme.decode(duplicate); throw ThemeError.invalid("duplicate keys accepted") }
        catch ThemeError.invalid(let message) where message.contains("중복") { count += 1 }
        let extra = String(decoding: beforeDemo, as: UTF8.self).replacingOccurrences(of: "\"schema\" : 1", with: "\"extra\": 1, \"schema\" : 1")
        do { _ = try LEDTheme.decode(Data(extra.utf8)); throw ThemeError.invalid("unknown keys accepted") }
        catch ThemeError.invalid(let message) where message.contains("누락") { count += 1 }
        let floating = String(decoding: beforeDemo, as: UTF8.self).replacingOccurrences(of: "\"schema\" : 1", with: "\"schema\" : 1.0")
        do { _ = try LEDTheme.decode(Data(floating.utf8)); throw ThemeError.invalid("float integer accepted") }
        catch ThemeError.invalid(let message) where message.contains("누락") { count += 1 }
        try duplicate.write(to: runtime.appendingPathComponent("theme.json"))
        let corrupt = ThemeStore(project: folder)
        await corrupt.refreshStatus()
        await corrupt.save()
        let backups = try FileManager.default.contentsOfDirectory(at: runtime, includingPropertiesForKeys: nil).filter { $0.lastPathComponent.hasPrefix("theme.recovery-") }
        let backupData = try backups.first.map { try Data(contentsOf: $0) }
        try check(backups.count == 1 && backupData == duplicate, "invalid original preserved before replacement")
        func fixture(_ object: [String: Any], _ filename: String) throws {
            try JSONSerialization.data(withJSONObject: object).write(to: runtime.appendingPathComponent(filename), options: .atomic)
        }
        let snapshot: [String: Any] = ["captured_at": now, "supported": true, "app_running": true, "slots": [["index": 1, "state": 2]]]
        try fixture(["updated_at": now, "transport": "host_writes_accepted_unverified", "snapshot": snapshot], "status.json")
        await loaded.refreshStatus()
        try check(loaded.liveFresh && loaded.slots[0].state == 2, "fresh supported state")
        var stale = snapshot; stale["captured_at"] = now - 60
        try fixture(["updated_at": now, "transport": "host_writes_accepted_unverified", "snapshot": stale], "status.json")
        await loaded.refreshStatus(); try check(!loaded.liveFresh, "fresh file cannot hide stale snapshot")
        try fixture(["updated_at": now, "transport": "stopped", "snapshot": snapshot], "status.json")
        await loaded.refreshStatus(); try check(!loaded.liveFresh, "stopped bridge is not live")
        let receipt: [String: Any] = ["schema": 1, "theme_protocol": 1, "verified_via": "usb_query", "verified_at": now - 1, "vendor_id": 13364, "product_id": 672]
        try fixture(receipt, "theme-firmware.json")
        await loaded.refreshStatus(); try check(loaded.firmwareReady, "valid capability receipt")
        var badReceipt = receipt; badReceipt["schema"] = true
        try fixture(badReceipt, "theme-firmware.json")
        await loaded.refreshStatus(); try check(!loaded.firmwareReady, "boolean is not receipt integer")
        badReceipt = receipt; badReceipt["verified_at"] = now + 86400
        try fixture(badReceipt, "theme-firmware.json")
        await loaded.refreshStatus(); try check(!loaded.firmwareReady, "future receipt rejected")
        let delayed = ThemeStore(project: folder, testReadDelay: 0.2)
        try check(!delayed.initialLoadFinished && delayed.slots.count == 12 && delayed.previewMode == .sample, "constructor shows sample without reading")
        let reading = Task { await delayed.refreshStatus() }
        try await Task.sleep(for: .milliseconds(35))
        try check(delayed.loadingFiles && !delayed.canSave, "pending file access cannot overwrite settings")
        delayed.select(state: 6)
        try check(delayed.selected.state == 6, "main actor stays responsive during pending file open")
        await reading.value
        try check(delayed.initialLoadFinished && !delayed.loadingFiles, "background read completed")
        delayed.apply(ThemeFilesSnapshot(theme: .failed("permission", denied: true), firmware: .missing, status: .missing))
        try check(delayed.fileAccess == .denied && !delayed.canSave && delayed.slots.count == 12, "permission denial retains responsive preview")
        await delayed.refreshStatus()
        delayed.draft.states[1].color = "#123456"
        var externallyEdited = delayed.saved
        externallyEdited.states[1].color = "#654321"
        externallyEdited.revision = UUID().uuidString.lowercased()
        try externallyEdited.encoded().write(to: runtime.appendingPathComponent("theme.json"), options: .atomic)
        let savedOverExternal = await delayed.save()
        try check(!savedOverExternal && delayed.draft.states[1].color == "#123456", "save rejects external change and preserves draft")
        await delayed.refreshStatus()
        try check(delayed.externalChange && !delayed.canSave, "external edits require reload")
        await delayed.refreshStatus(discardEdits: true)
        try check(!delayed.externalChange && delayed.draft.states[1].color == "#654321", "explicit reload adopts external settings")
        delayed.draft.states[1].color = "#112233"
        let writing = Task { await delayed.save() }
        try await Task.sleep(for: .milliseconds(35))
        try check(delayed.saving, "save waits off the main actor")
        delayed.draft.states[1].color = "#445566"
        let writeSucceeded = await writing.value
        try check(writeSucceeded && delayed.saved.states[1].color == "#112233" && delayed.draft.states[1].color == "#445566" && delayed.dirty, "save completion preserves newer edits")
        count += try SettingsRender.metadataFilterSelfTest()
        print("Settings self-tests passed: \(count)")
    }
}
