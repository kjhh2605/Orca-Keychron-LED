import SwiftUI

struct StateEditor: View {
    @Binding var state: LEDStateTheme
    @Binding var globalBrightness: Int
    @State private var pickedColor = Color.blue
    @State private var hexText = ""
    @State private var hexValid = true
    @FocusState private var hexFocused: Bool
    private var brightnessBinding: Binding<Double> {
        Binding(get: { Double(state.brightness) }, set: { state.brightness = Int($0.rounded()) })
    }
    private var globalBinding: Binding<Double> {
        Binding(get: { Double(globalBrightness) }, set: { globalBrightness = Int($0.rounded()) })
    }
    private var periodBinding: Binding<Double> {
        Binding(get: { Double(state.period_ms) }, set: { state.period_ms = Int($0.rounded()) })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 11) {
                Image(systemName: state.symbol).font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(state.displayColor)
                    .frame(width: 38, height: 38)
                    .background(state.displayColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
                VStack(alignment: .leading, spacing: 4) {
                    Text(state.label).font(.system(size: 18, weight: .semibold))
                    Text(state.detail).font(.system(size: 11)).foregroundStyle(SettingsStyle.muted)
                }
                Spacer()
                Text("0\(state.state)").font(.system(size: 25, weight: .light, design: .monospaced)).foregroundStyle(.white.opacity(0.12))
            }
            HStack(alignment: .top, spacing: 24) {
                VStack(alignment: .leading, spacing: 10) {
                    fieldTitle("색상")
                    HStack(spacing: 10) {
                        ColorPicker("상태 색상", selection: $pickedColor, supportsOpacity: false)
                            .labelsHidden().frame(width: 34)
                        TextField("#RRGGBB", text: $hexText)
                            .font(.system(size: 12, design: .monospaced)).textFieldStyle(.plain)
                            .focused($hexFocused)
                            .padding(.horizontal, 10).frame(width: 98, height: 31)
                            .background(SettingsStyle.background, in: RoundedRectangle(cornerRadius: 6))
                            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(hexValid ? SettingsStyle.line : Color.red))
                            .accessibilityLabel("16진수 색상 코드")
                            .onSubmit { normalizeHex() }
                    }
                    Text(hexValid ? "6자리 색상 코드를 입력할 수 있어요." : "# 다음에 0–9, A–F 6자리를 입력하세요.")
                        .font(.system(size: 9)).foregroundStyle(hexValid ? SettingsStyle.muted : .red)
                }.frame(width: 225, alignment: .leading)
                VStack(alignment: .leading, spacing: 10) {
                    HStack { fieldTitle("상태 밝기"); Spacer(); valueText("\(state.brightness)%") }
                    Slider(value: brightnessBinding, in: 0...100, step: 1)
                        .accessibilityLabel("\(state.label) 밝기").accessibilityValue("\(state.brightness)퍼센트")
                    Text("이 상태의 조명에만 적용됩니다.").font(.system(size: 9)).foregroundStyle(SettingsStyle.muted)
                }
            }
            HStack(alignment: .top, spacing: 24) {
                VStack(alignment: .leading, spacing: 9) {
                    fieldTitle("조명 효과")
                    Picker("조명 효과", selection: $state.effect) {
                        ForEach(LEDEffect.allCases) { effect in Text(effect.label).tag(effect) }
                    }.labelsHidden().pickerStyle(.menu)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.frame(width: 225, alignment: .leading)
                VStack(alignment: .leading, spacing: 9) {
                    HStack { fieldTitle("반복 주기"); Spacer(); valueText(String(format: "%.1f초", Double(state.period_ms) / 1000)) }
                    Slider(value: periodBinding, in: 800...10000, step: 100)
                        .disabled(!state.effect.animated)
                        .accessibilityLabel("효과 반복 주기").accessibilityValue(String(format: "%.1f초", Double(state.period_ms) / 1000))
                    HStack { Text("빠르게"); Spacer(); Text("느리게") }.font(.system(size: 9)).foregroundStyle(SettingsStyle.muted)
                }
            }
            Divider().overlay(SettingsStyle.line)
            HStack(spacing: 18) {
                VStack(alignment: .leading, spacing: 4) {
                    fieldTitle("전체 밝기")
                    Text("모든 F키에 함께 적용").font(.system(size: 9)).foregroundStyle(SettingsStyle.muted)
                }.frame(width: 135, alignment: .leading)
                Image(systemName: "sun.min").font(.system(size: 11)).foregroundStyle(SettingsStyle.muted)
                Slider(value: globalBinding, in: 0...255, step: 1)
                    .accessibilityLabel("전체 키보드 밝기").accessibilityValue("\(Int((Double(globalBrightness) / 255 * 100).rounded()))퍼센트")
                Image(systemName: "sun.max").font(.system(size: 14)).foregroundStyle(SettingsStyle.muted)
                valueText("\(Int((Double(globalBrightness) / 255 * 100).rounded()))%").frame(width: 36, alignment: .trailing)
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity)
        .frame(height: 335)
        .background(SettingsStyle.panel, in: RoundedRectangle(cornerRadius: 15))
        .overlay(RoundedRectangle(cornerRadius: 15).strokeBorder(SettingsStyle.line))
        .onAppear { pickedColor = state.displayColor; hexText = state.color }
        .onChange(of: pickedColor) { _, color in
            guard let hex = LEDStateTheme.hex(from: color), hex != state.color else { return }
            state.color = hex
            hexText = hex
            hexValid = true
        }
        .onChange(of: hexText) { _, value in
            if let hex = LEDStateTheme.canonicalHex(value) {
                hexValid = true
                state.color = hex
                pickedColor = state.displayColor
            } else { hexValid = false }
        }
        .onChange(of: hexFocused) { _, focused in if !focused { normalizeHex() } }
        .onChange(of: state.color) { _, hex in
            if !hexFocused { hexText = hex; pickedColor = state.displayColor; hexValid = true }
        }
    }
    private func normalizeHex() {
        // An incomplete text edit never replaces the last valid saved color.
        hexText = state.color
        hexValid = true
    }
    private func fieldTitle(_ title: String) -> some View { Text(title).font(.system(size: 11, weight: .medium)) }
    private func valueText(_ text: String) -> some View {
        Text(text).font(.system(size: 11, weight: .medium, design: .monospaced)).foregroundStyle(SettingsStyle.muted)
    }
}
