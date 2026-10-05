import AppKit
import SwiftUI

@MainActor final class SettingsAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var window: NSWindow?
    var store: ThemeStore?
    private var allowCloseAfterSave = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Inside a source checkout, share its runtime/ folder; a downloaded app
        // (standalone or embedded in Orca LED.app) uses Application Support.
        let appParent = Bundle.main.bundleURL.deletingLastPathComponent()
        let checkout = appParent.lastPathComponent == "build" ? appParent.deletingLastPathComponent() : appParent
        let project = FileManager.default.fileExists(atPath: checkout.appendingPathComponent("orca_led/__main__.py").path)
            ? checkout
            : FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Orca LED", isDirectory: true)
        let demo = CommandLine.arguments.contains("--demo") || CommandLine.arguments.contains("--preview-only")
        let store = ThemeStore(project: project, demoOnly: demo)
        self.store = store
        makeMenu()
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1040, height: 758),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Orca LED · 색상과 효과"
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(red: 0.063, green: 0.068, blue: 0.082, alpha: 1)
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentMinSize = NSSize(width: 930, height: 710)
        window.contentView = NSHostingView(rootView: SettingsView(store: store))
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.center()
        window.makeKeyAndOrderFront(nil)
        self.window = window
        NSApp.activate(ignoringOtherApps: true)
        window.displayIfNeeded()
        DispatchQueue.main.async { store.startPolling() }
    }

    private func makeMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Orca LED Settings 종료", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "편집")
        edit.addItem(withTitle: "실행 취소", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "다시 실행", action: Selector(("redo:")), keyEquivalent: "Z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "잘라내기", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "복사", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "붙여넣기", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "모두 선택", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        NSApp.mainMenu = main
    }

    private func mayClose() -> Bool {
        guard !allowCloseAfterSave, let store, store.dirty || store.saving, !store.demoOnly else { return true }
        let alert = NSAlert()
        alert.messageText = "변경한 설정을 저장할까요?"
        alert.informativeText = "아직 저장하지 않은 색상과 효과가 있습니다."
        alert.addButton(withTitle: "저장")
        alert.addButton(withTitle: "계속 편집")
        alert.addButton(withTitle: "저장하지 않음")
        alert.buttons.first?.isEnabled = store.canSave
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            Task { [weak self] in
                if await store.save(), !store.dirty {
                    self?.allowCloseAfterSave = true
                    NSApp.terminate(nil)
                }
            }
            return false
        case .alertThirdButtonReturn: store.discard(); return true
        default: return false
        }
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { mayClose() }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply { mayClose() ? .terminateNow : .terminateCancel }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { store?.stopPolling() }
}

@main enum SettingsMain {
    @MainActor static func main() {
        if let index = CommandLine.arguments.firstIndex(of: "--render-demo") {
            do {
                guard CommandLine.arguments.indices.contains(index + 1) else { throw ThemeError.invalid("--render-demo requires an output PNG path") }
                _ = NSApplication.shared
                try SettingsRender.writeDemo(to: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
            } catch {
                FileHandle.standardError.write(Data("Settings demo render failed: \(error)\n".utf8))
                exit(1)
            }
            return
        }
        if CommandLine.arguments.contains("--self-test") {
            Task { @MainActor in
                do { try await SettingsSelfTests.run(); exit(0) }
                catch {
                    FileHandle.standardError.write(Data("Settings self-test failed: \(error)\n".utf8))
                    exit(1)
                }
            }
            RunLoop.main.run()
            return
        }
        let application = NSApplication.shared
        let delegate = SettingsAppDelegate()
        application.delegate = delegate
        application.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { application.run() }
    }
}
