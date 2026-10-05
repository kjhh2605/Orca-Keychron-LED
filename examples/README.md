# 공개 예제

`theme.json`은 기본 색상과 효과를 담은 합성 예제입니다. 실제 사용자의 설정이나 기기에서 읽은 데이터가 아닙니다. 상태 번호는 1 대기, 2 진행 중, 3 질문·승인 대기, 4 완료, 5 오류, 6 확인 불가입니다. 미할당(0)은 항상 꺼집니다.

- `global_brightness`: 0–255, 전체 밝기
- `brightness`: 0–100, 상태별 밝기 비율
- `color`: `#RRGGBB`
- `effect`: `solid`, `breathe`, `blink`, `double`, `triple`, `off`
- `period_ms`: 800–10000ms, 100ms 단위
- `revision`: 변경할 때마다 새 UUID. 설정 앱은 저장할 때 자동으로 갱신합니다.

설정 앱이 `runtime/theme.json`을 관리하므로 일반 사용자는 앱에서 조정하면 됩니다. 이 예제는 형식 설명과 개발용이며 자동으로 기기에 전송되지 않습니다.
