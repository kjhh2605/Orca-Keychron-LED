import SwiftUI

private struct StaticPreviewKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    var renderStaticPreview: Bool {
        get { self[StaticPreviewKey.self] }
        set { self[StaticPreviewKey.self] = newValue }
    }
}

enum SettingsStyle {
    static let background = Color(red: 0.063, green: 0.068, blue: 0.082)
    static let panel = Color(red: 0.098, green: 0.105, blue: 0.123)
    static let elevated = Color(red: 0.137, green: 0.144, blue: 0.167)
    static let accent = Color(red: 1, green: 0.67, blue: 0.37)
    static let muted = Color(red: 0.62, green: 0.65, blue: 0.71)
    static let line = Color.white.opacity(0.085)
}

struct SettingsView: View {
    @Bindable var store: ThemeStore
    @State private var showReset = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            header
            KeyboardPreview(store: store)
            HStack(alignment: .top, spacing: 16) {
                stateList
                StateEditor(state: $store.draft.states[store.selectedIndex], globalBrightness: $store.draft.global_brightness)
                    .id("\(store.selectedIndex)-\(store.editGeneration)")
            }
            footer
        }
        .padding(28)
        .frame(minWidth: 930, minHeight: 710)
        .background(SettingsStyle.background)
        .preferredColorScheme(.dark)
        .tint(SettingsStyle.accent)
        .onDisappear { store.stopPolling() }
        .confirmationDialog("모든 상태를 기본 색상과 효과로 되돌릴까요?", isPresented: $showReset, titleVisibility: .visible) {
            Button("기본값 미리보기") { store.restoreDefaults() }
            Button("취소", role: .cancel) {}
        } message: { Text("저장하기 전까지 키보드 설정은 바뀌지 않습니다.") }
    }

    private var header: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 7) {
                HStack(spacing: 7) {
                    Image(systemName: "keyboard").foregroundStyle(SettingsStyle.accent)
                    Text("ORCA LED").tracking(2.5)
                    Text("/  K10 PRO").foregroundStyle(SettingsStyle.muted).tracking(1.6)
                }.font(.system(size: 10, weight: .semibold, design: .monospaced))
                Text("상태를 색으로").font(.system(size: 29, weight: .semibold))
                Text("눈에 잘 들어오는 색과 움직임으로, 작업의 흐름을 읽어요.")
                    .font(.system(size: 12)).foregroundStyle(SettingsStyle.muted)
            }
            Spacer()
            HStack(spacing: 6) {
                Circle().fill(store.liveFresh ? Color.green : SettingsStyle.muted).frame(width: 6, height: 6)
                Text(store.demoOnly ? "화면 미리보기" : store.liveFresh ? "Orca 연동 중" : "연동 대기")
                    .font(.system(size: 11, weight: .medium))
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
            .background(SettingsStyle.panel, in: Capsule())
        }
    }

    private var stateList: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("상태별 설정").font(.system(size: 11, weight: .semibold)).foregroundStyle(SettingsStyle.muted)
                .padding(.leading, 11).padding(.bottom, 3)
            ForEach(Array(store.draft.states.enumerated()), id: \.element.id) { index, state in
                Button { store.selectedIndex = index } label: {
                    HStack(spacing: 10) {
                        RoundedRectangle(cornerRadius: 4).fill(state.displayColor).frame(width: 10, height: 10)
                            .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(.white.opacity(0.15)))
                        Text(state.label).font(.system(size: 12, weight: store.selectedIndex == index ? .semibold : .regular))
                        Spacer(minLength: 5)
                        if store.selectedIndex == index { Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold)) }
                    }
                    .padding(.horizontal, 12).frame(height: 37)
                    .foregroundStyle(store.selectedIndex == index ? .white : SettingsStyle.muted)
                    .background(store.selectedIndex == index ? SettingsStyle.elevated : .clear, in: RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(state.label) 설정")
                .accessibilityAddTraits(store.selectedIndex == index ? .isSelected : [])
            }
            Divider().overlay(SettingsStyle.line).padding(.vertical, 3)
            HStack(spacing: 9) {
                Image(systemName: "minus").font(.system(size: 10))
                Text("미할당 키는 항상 꺼짐").font(.system(size: 10))
            }.foregroundStyle(SettingsStyle.muted).padding(.horizontal, 12)
        }
        .padding(14)
        .frame(width: 221)
        .frame(height: 335, alignment: .top)
        .background(SettingsStyle.panel, in: RoundedRectangle(cornerRadius: 15))
        .overlay(RoundedRectangle(cornerRadius: 15).strokeBorder(SettingsStyle.line))
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .center, spacing: 9) {
                Image(systemName: store.firmwareReady ? "checkmark.shield" : "info.circle")
                    .foregroundStyle(SettingsStyle.accent)
                VStack(alignment: .leading, spacing: 3) {
                    Text(store.accessNoticeTitle ?? (store.demoOnly ? "내 취향대로 조정하고, 화면에서 먼저 확인하세요." : store.firmwareReady ? "색상 설정을 지원하는 펌웨어가 확인됐습니다." : "키보드 색상 조정에는 펌웨어 업데이트가 필요합니다."))
                        .font(.system(size: 11, weight: .medium))
                    Text(store.accessNoticeDetail ?? (store.demoOnly ? "이 화면은 예시 상태입니다. 실제 키보드는 변경되지 않습니다." : store.firmwareReady ? "전체 전송에는 약 2분이 걸릴 수 있어요. 실제 조명으로 적용 여부를 확인해 주세요." : "설정은 미리 저장할 수 있어요. 기존 상태 조명은 계속 사용할 수 있습니다."))
                        .font(.system(size: 10)).foregroundStyle(SettingsStyle.muted)
                }
                Spacer()
                if !store.demoOnly && (store.fileAccess != .ready || store.externalChange) {
                    Button(store.externalChange ? "다시 불러오기" : "다시 확인") {
                        Task { await store.refreshStatus(discardEdits: store.externalChange) }
                    }.disabled(store.loadingFiles || store.saving).controlSize(.small)
                }
                if let date = store.firmwareVerifiedAt, store.firmwareReady {
                    Text(date, format: .dateTime.month().day().hour().minute())
                        .font(.system(size: 10)).foregroundStyle(SettingsStyle.muted)
                        .help("마지막 USB 확인 시각")
                }
            }
            HStack(spacing: 12) {
                Button("기본값 복원") { showReset = true }.buttonStyle(.plain).font(.system(size: 11))
                    .foregroundStyle(SettingsStyle.muted)
                Spacer()
                VStack(alignment: .trailing, spacing: 3) {
                    Text(store.error ?? store.deliveryTitle)
                        .foregroundStyle(store.error != nil ? Color.red.opacity(0.9) : SettingsStyle.muted)
                        .font(.system(size: 10))
                        .lineLimit(2)
                    if let notice = store.notice, store.error == nil {
                        Text(notice).font(.system(size: 9)).foregroundStyle(SettingsStyle.muted).lineLimit(1)
                    }
                }
                Button("변경 취소") { store.discard() }.disabled(!store.dirty || store.saving).keyboardShortcut(.escape, modifiers: [])
                Button(store.saving ? "저장 중…" : "저장 및 적용") { Task { await store.save() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(!store.canSave)
                    .keyboardShortcut("s", modifiers: .command)
                    .help("설정을 저장합니다. 연동 중이면 키보드 전송을 요청합니다.")
            }
        }
        .padding(.top, 1)
    }
}

struct KeyboardPreview: View {
    @Bindable var store: ThemeStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.renderStaticPreview) private var renderStaticPreview
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                HStack(spacing: 7) {
                    Text("F1 — F12").font(.system(size: 10, weight: .semibold, design: .monospaced)).tracking(1)
                    Text("화면 미리보기").font(.system(size: 10)).foregroundStyle(SettingsStyle.muted)
                    if !store.liveFresh && store.previewMode == .live {
                        Text(store.liveSlots.isEmpty ? "· 실행 기록 없음" : "· 마지막 기록").font(.system(size: 10)).foregroundStyle(SettingsStyle.muted)
                    }
                }
                Spacer()
                if !store.demoOnly {
                    Picker("미리보기 상태", selection: $store.previewMode) {
                        ForEach(ThemeStore.PreviewMode.allCases) { mode in Text(mode.rawValue).tag(mode) }
                    }.labelsHidden().pickerStyle(.segmented).frame(width: 152).controlSize(.small)
                }
            }
            TimelineView(.animation(minimumInterval: 1.0 / 24, paused: reduceMotion || renderStaticPreview)) { context in
                HStack(spacing: 9) {
                    ForEach(store.slots) { slot in
                        PreviewKey(slot: slot, theme: store.draft.state(slot.state), globalBrightness: store.draft.global_brightness,
                                   seconds: context.date.timeIntervalSinceReferenceDate, reduceMotion: reduceMotion || renderStaticPreview,
                                   selected: slot.state == store.selected.state) { store.select(state: slot.state) }
                    }
                }
            }
            HStack {
                Text("키를 눌러 해당 상태를 편집하세요. 화면과 실제 LED의 색감은 다를 수 있어요.")
                Spacer()
                if renderStaticPreview { Text("정지 화면 미리보기") }
                else if reduceMotion { Text("움직임 줄이기 사용 중") }
            }.font(.system(size: 9)).foregroundStyle(SettingsStyle.muted)
        }
        .padding(17)
        .background(SettingsStyle.panel, in: RoundedRectangle(cornerRadius: 15))
        .overlay(RoundedRectangle(cornerRadius: 15).strokeBorder(SettingsStyle.line))
    }
}

struct PreviewKey: View {
    let slot: PreviewSlot
    let theme: LEDStateTheme?
    let globalBrightness: Int
    let seconds: TimeInterval
    let reduceMotion: Bool
    let selected: Bool
    let action: () -> Void
    private var color: Color { theme?.displayColor ?? .clear }
    private var intensity: Double {
        guard let theme else { return 0 }
        return theme.effect.amplitude(at: seconds, period: theme.period_ms, reduceMotion: reduceMotion)
            * Double(theme.brightness) / 100 * Double(globalBrightness) / 255
    }
    var body: some View {
        Button(action: action) {
            VStack(spacing: 7) {
                ZStack(alignment: .bottom) {
                    RoundedRectangle(cornerRadius: 9)
                        .fill(LinearGradient(colors: [SettingsStyle.elevated, SettingsStyle.background], startPoint: .top, endPoint: .bottom))
                        .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(selected && theme != nil ? color.opacity(0.6) : SettingsStyle.line, lineWidth: 1))
                    RoundedRectangle(cornerRadius: 7).fill(color.opacity(intensity * 0.46)).blur(radius: 6).padding(5)
                    Text("F\(slot.id)").font(.system(size: 13, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.white.opacity(slot.state == 0 ? 0.3 : 0.94)).frame(maxWidth: .infinity, maxHeight: .infinity)
                    Capsule().fill(color.opacity(intensity)).frame(height: 3).padding(.horizontal, 13).padding(.bottom, 7)
                }.frame(height: 59)
                Text(LEDStateTheme.labels[slot.state] ?? "확인 불가")
                    .font(.system(size: 8)).foregroundStyle(SettingsStyle.muted).lineLimit(1).minimumScaleFactor(0.8)
            }.frame(maxWidth: .infinity)
        }
        .buttonStyle(.plain)
        .disabled(slot.state == 0)
        .accessibilityLabel("F\(slot.id), \(LEDStateTheme.labels[slot.state] ?? "확인 불가")")
        .accessibilityHint(slot.state == 0 ? "할당되지 않은 키입니다." : "이 상태의 색상과 효과를 편집합니다.")
        .help("F\(slot.id) · \(LEDStateTheme.labels[slot.state] ?? "확인 불가")")
    }
}
