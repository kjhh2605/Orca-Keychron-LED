#!/bin/zsh
# Run this manually only after finishing/saving work and quitting Orca yourself.
# This script never quits, kills, reloads, or modifies an existing application.
set -eu

orca_app='/Applications/Orca.app'
orca_debug_port=19347
# The bridge's single list of live-verified Orca versions.
compat_file="${0:A:h:h}/orca_led/orca_compat.json"

orca_process_check=0
/usr/bin/pgrep -x Orca >/dev/null 2>&1 || orca_process_check=$?
if [[ "$orca_process_check" == 0 ]]; then
  print -u2 'Orca가 실행 중입니다. 진행 중인 작업을 정리하고 직접 종료한 뒤 다시 실행하세요.'
  print -u2 '이 스크립트는 Orca나 에이전트를 자동으로 종료하지 않습니다.'
  exit 1
fi
if [[ "$orca_process_check" != 1 ]]; then
  print -u2 'Orca 실행 여부를 확인할 수 없어 시작을 중단했습니다.'
  exit 1
fi
# A restricted caller can receive an empty pgrep result despite running apps.
# Require a readable system process list before treating absence as confirmed.
if ! orca_process_snapshot=$(/bin/ps -A -o comm=); then
  print -u2 '프로세스 목록을 읽을 수 없어 Orca 시작을 중단했습니다.'
  exit 1
fi
if [[ "$orca_process_snapshot" != *'/sbin/launchd'* ]]; then
  print -u2 '전체 프로세스 목록을 확인할 수 없어 Orca 시작을 중단했습니다.'
  exit 1
fi
if [[ "$orca_process_snapshot" == *'/Applications/Orca.app/Contents/MacOS/Orca'* ]]; then
  print -u2 'Orca가 아직 실행 중입니다. 직접 종료한 뒤 다시 실행하세요.'
  exit 1
fi
if [[ ! -d "$orca_app" ]]; then
  print -u2 '지원하는 Orca 설치 경로를 찾을 수 없습니다: /Applications/Orca.app'
  exit 1
fi
if ! orca_version=$(/usr/bin/plutil -extract CFBundleShortVersionString raw -o - "$orca_app/Contents/Info.plist"); then
  print -u2 'Orca 설치 버전을 읽을 수 없어 시작을 중단했습니다.'
  exit 1
fi
if [[ ! "$orca_version" =~ '^[0-9]+(\.[0-9]+){1,3}$' ]]; then
  print -u2 "Orca 설치 버전을 확인할 수 없어 시작을 중단했습니다: $orca_version"
  exit 1
fi
# Unverified versions still open: the bridge shows read-only LED status if it
# can map Orca's screen, and keeps F-key workspace switching off.
verified_versions=$(/usr/bin/plutil -extract verified_versions json -o - "$compat_file" 2>/dev/null) || verified_versions=''
if [[ "$verified_versions" != *"\"$orca_version\""* ]]; then
  print -u2 "Orca $orca_version 은(는) 아직 검증되지 않은 버전입니다 (미검증)."
  print -u2 'LED 상태 표시는 시도하지만 F키 작업공간 이동은 꺼집니다. 확인 방법: ./scripts/verify-orca-version.py'
fi
if /usr/sbin/lsof -nP -iTCP:"$orca_debug_port" -sTCP:LISTEN >/dev/null 2>&1; then
  print -u2 "로컬 연결 포트 $orca_debug_port 가 이미 사용 중입니다. 실행을 중단했습니다."
  exit 1
fi

print 'Orca를 로컬 연동 모드로 시작합니다. 설치 파일은 변경하지 않습니다.'
print '이 모드에서는 이 Mac의 다른 프로세스도 Orca 디버깅 포트에 접근할 수 있습니다.'
print '연동을 사용하지 않을 때는 Orca를 직접 종료하고 평소 방법으로 다시 여세요.'
/usr/bin/open -a "$orca_app" --args \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port="$orca_debug_port"
print '시작 요청을 보냈습니다. 연동 프로그램의 상태 확인으로 연결을 검증하세요.'
