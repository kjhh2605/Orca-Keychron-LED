#!/bin/sh
# Print the codesign identity for local builds. Uses the self-signed
# "Orca LED Local Signing" certificate when it is installed and trusted
# (scripts/create-signing-identity.sh), otherwise "-" (ad-hoc). A stable
# certificate lets macOS keep Input Monitoring and Accessibility permissions
# across rebuilds; an ad-hoc signature changes with every build.
# ORCA_LED_SIGN_IDENTITY overrides the choice (e.g. a Developer ID identity).
set -eu
NAME="Orca LED Local Signing"
if [ -n "${ORCA_LED_SIGN_IDENTITY:-}" ]; then
  printf '%s\n' "$ORCA_LED_SIGN_IDENTITY"
  exit 0
fi
HASH=$(security find-identity -v -p codesigning 2>/dev/null | awk -v name="\"$NAME\"" 'index($0, name) { print $2; exit }')
if [ -n "$HASH" ]; then
  printf '%s\n' "$HASH"
else
  printf '%s\n' "주의: '$NAME' 인증서가 없어 ad-hoc 서명합니다. 다시 빌드할 때마다 macOS 권한을 다시 허용해야 합니다. (scripts/create-signing-identity.sh)" >&2
  printf '%s\n' "-"
fi
