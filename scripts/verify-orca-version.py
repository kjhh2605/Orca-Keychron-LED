#!/usr/bin/env python3
"""Check the installed Orca against this bridge and record it as verified.

Runs bundle discovery (always), the local runtime RPC checks (when Orca is
running) and the live renderer read (only when Orca is already open in
debugging mode). It never launches, quits or restarts Orca. The version is
added to orca_led/orca_compat.json only after every check ran and passed.
"""
from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orca_led.cdp_ui import DEFAULT_CDP_PORT, CDPUIProvider  # noqa: E402
from orca_led.orca import OrcaClient, OrcaError, OrcaRPC  # noqa: E402
from orca_led.orca_compat import COMPAT_FILE, CompatError, discover_installed, is_verified, mark_verified  # noqa: E402


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT)
    parser.add_argument("--dry-run", action="store_true", help="Run the checks without updating orca_compat.json")
    args = parser.parse_args()

    try:
        layout = discover_installed()
    except CompatError as error:
        print(f"[실패] 설치된 Orca 분석: {error.code} · {error}")
        return 1
    version = layout["version"]
    print(f"설치된 Orca: {version} ({'검증됨' if is_verified(version) else '미검증'})")
    print(f"[통과] Cmd+숫자 순서 함수: {layout['order_module']} → {layout['order_export']}")
    print(f"[통과] 작업공간 저장소: {layout['store_module']} → {layout['store_export']}")

    rpc = OrcaRPC()
    try:
        status = rpc.call("status.get")
        if status.get("appVersion") != version:
            print(f"[실패] 실행 중인 Orca {status.get('appVersion')} 이(가) 설치본 {version}과 다릅니다. Orca를 재시작한 뒤 다시 확인하세요.")
            return 1
        inventory = OrcaClient(rpc=rpc).inventory()
        print(f"[통과] 로컬 런타임 RPC: 작업공간 {len(inventory['workspaces'])}개")
    except OrcaError as error:
        if error.code not in {"runtime_unavailable", "runtime_closed", "runtime_timeout"}:
            print(f"[실패] 로컬 런타임 RPC: {error.code} · {error}")
            return 1
        print(f"[건너뜀] 로컬 런타임 RPC: {error.code} · Orca를 실행한 뒤 다시 확인하세요.")
        print("검증 기록을 바꾸지 않았습니다.")
        return 2
    if not port_open(args.cdp_port):
        print(f"[건너뜀] 화면 읽기(CDP 127.0.0.1:{args.cdp_port}): 연동 모드가 아닙니다.")
        print("Orca LED.app 또는 ./scripts/start-orca-debug.command로 Orca를 연동 모드로 연 뒤 다시 실행하세요.")
        print("검증 기록을 바꾸지 않았습니다.")
        return 2
    snapshot = OrcaClient(ui_provider=CDPUIProvider(port=args.cdp_port, rpc=rpc), rpc=rpc).snapshot()
    if not snapshot.get("supported"):
        print(f"[실패] 화면 읽기: {snapshot.get('reason')} · {snapshot.get('message')}")
        return 1
    print(f"[통과] 화면 읽기: Cmd+숫자 작업공간 {snapshot['total_slots']}개, 현재 선택 {snapshot.get('selected_id') or '없음'}")

    if args.dry_run:
        print("모든 확인을 통과했습니다 (--dry-run: 검증 기록을 바꾸지 않음).")
    elif mark_verified(version):
        print(f"모든 확인을 통과해 Orca {version}을(를) {COMPAT_FILE}에 검증 버전으로 추가했습니다.")
    else:
        print(f"모든 확인을 통과했습니다. Orca {version}은(는) 이미 검증 버전입니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
