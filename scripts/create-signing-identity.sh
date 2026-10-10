#!/bin/sh
# One-time setup: create a self-signed code signing certificate in the login
# keychain and trust it for code signing, so rebuilt apps keep one signing
# identity and macOS keeps their privacy permissions. Local use only: other
# Macs do not trust this certificate and it does not replace notarization.
# Remove it with Keychain Access (login → My Certificates → Orca LED Local Signing).
set -eu
NAME="Orca LED Local Signing"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -v -p codesigning | grep -q "\"$NAME\""; then
  printf '%s\n' "'$NAME' 인증서가 이미 설치되어 있습니다."
  exit 0
fi

umask 077
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

if security find-certificate -c "$NAME" -p "$KEYCHAIN" > "$WORK/cert.pem" 2>/dev/null; then
  printf '%s\n' "기존 '$NAME' 인증서를 찾았습니다. 신뢰 설정만 진행합니다."
else
  cat > "$WORK/req.cnf" <<CNF
[req]
distinguished_name = dn
prompt = no
x509_extensions = v3
[dn]
CN = $NAME
[v3]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
CNF
  # /usr/bin/openssl (LibreSSL) writes a PKCS#12 file that security(1) imports.
  /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$WORK/req.cnf" \
    -keyout "$WORK/key.pem" -out "$WORK/cert.pem" 2>/dev/null
  PASS=$(/usr/bin/openssl rand -hex 16)
  /usr/bin/openssl pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/cert.pem" \
    -out "$WORK/identity.p12" -passout "pass:$PASS"
  security import "$WORK/identity.p12" -k "$KEYCHAIN" -P "$PASS" -T /usr/bin/codesign
fi

printf '%s\n' "macOS가 암호를 물으면 로그인 암호를 입력해 이 인증서를 코드 서명용으로 신뢰하도록 허용하세요."
security add-trusted-cert -p codeSign -k "$KEYCHAIN" "$WORK/cert.pem"

if security find-identity -v -p codesigning | grep "\"$NAME\""; then
  printf '%s\n' "완료. 이제 빌드 스크립트가 이 인증서로 서명합니다."
else
  printf '%s\n' "인증서가 코드 서명용으로 유효하지 않습니다. 키체인 접근에서 신뢰 설정을 확인해 주세요." >&2
  exit 1
fi
