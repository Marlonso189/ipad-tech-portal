#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build iPad Tech Portal.app, then a .pkg and a .dmg.
# Signing and notarization run only when the matching secrets are set.
# A missing Developer ID Application certificate still produces an unsigned
# pkg and dmg and exits 0. A certificate that is present but invalid fails.

set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

APP_NAME="iPad Tech Portal.app"
APP="$root/dist/$APP_NAME"
PKG="$root/dist/ipad-tech-portal-mac.pkg"
DMG="$root/dist/ipad-tech-portal-mac.dmg"
ENTITLEMENTS="$root/macos/entitlements.plist"
WORKDIR="$(mktemp -d)"

cleanup() {
  if [[ -n "${KEYCHAIN:-}" && -f "$KEYCHAIN" ]]; then
    security delete-keychain "$KEYCHAIN" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt -r requirements-desktop.txt
python3 -m PyInstaller --noconfirm --clean macos/ipad-tech-portal.spec

if [[ ! -d "$APP" ]]; then
  echo "PyInstaller did not produce $APP" >&2
  exit 1
fi

VERSION="$(python3 -c "import sys; sys.path.insert(0, '.'); from app import __version__; print(__version__)")"
BINARY=""
while IFS= read -r candidate; do
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    BINARY="$candidate"
    break
  fi
done < <(find "$APP/Contents/MacOS" -type f)
if [[ -z "$BINARY" ]]; then
  echo "The app bundle has no executable." >&2
  exit 1
fi
if ! file "$BINARY" | grep -q "arm64"; then
  echo "Expected an Apple Silicon (arm64) executable, got: $(file "$BINARY")" >&2
  exit 1
fi

SIGNED_APP=0
APP_IDENTITY=""
if [[ -z "${MACOS_CERTIFICATE_APPLICATION_P12:-}" ]]; then
  echo "MACOS_CERTIFICATE_APPLICATION_P12 is not set. Building an unsigned app, pkg, and dmg."
else
  echo "Signing the app with the Developer ID Application certificate."
  KEYCHAIN_PASS="$(python3 -c "import secrets; print(secrets.token_hex(16))")"
  KEYCHAIN="$WORKDIR/build.keychain-db"
  security create-keychain -p "$KEYCHAIN_PASS" "$KEYCHAIN"
  security set-keychain-settings -lut 21600 "$KEYCHAIN"
  security unlock-keychain -p "$KEYCHAIN_PASS" "$KEYCHAIN"
  security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | sed 's/"//g')
  echo "$MACOS_CERTIFICATE_APPLICATION_P12" | base64 -D > "$WORKDIR/application.p12"
  security import "$WORKDIR/application.p12" -k "$KEYCHAIN" -P "${MACOS_CERTIFICATE_APPLICATION_PASSWORD:-}" -T /usr/bin/codesign -T /usr/bin/security
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASS" "$KEYCHAIN" >/dev/null
  APP_IDENTITY="$(security find-identity -v -p codesigning "$KEYCHAIN" | sed -n 's/.*"\(Developer ID Application:[^"]*\)".*/\1/p' | head -n 1)"
  if [[ -z "$APP_IDENTITY" ]]; then
    echo "The application certificate did not yield a Developer ID Application identity." >&2
    exit 1
  fi
  python3 - "$APP" "$ENTITLEMENTS" "$APP_IDENTITY" <<'PY'
import subprocess
import sys
from pathlib import Path

app = Path(sys.argv[1])
entitlements = sys.argv[2]
identity = sys.argv[3]

def sign(path: Path, extra: list[str] | None = None) -> None:
    command = [
        "codesign",
        "--force",
        "--options",
        "runtime",
        "--timestamp",
        "--sign",
        identity,
    ]
    if extra:
        command.extend(extra)
    command.append(str(path))
    subprocess.run(command, check=True)

macho = []
for path in app.rglob("*"):
    if not path.is_file() or path.is_symlink():
        continue
    kind = subprocess.run(["file", "-b", str(path)], check=True, capture_output=True, text=True).stdout
    if "Mach-O" in kind:
        macho.append(path)
for path in sorted(macho, key=lambda item: len(item.parts), reverse=True):
    sign(path)

frameworks = [path for path in app.rglob("*.framework") if path.is_dir()]
for path in sorted(frameworks, key=lambda item: len(item.parts), reverse=True):
    sign(path)

sign(app, ["--entitlements", entitlements])
PY
  codesign --verify --strict --verbose=2 "$APP"
  SIGNED_APP=1
fi

rm -rf "$root/dist/pkgroot" "$root/dist/dmg-stage"
mkdir -p "$root/dist/pkgroot" "$root/dist/dmg-stage"
cp -R "$APP" "$root/dist/pkgroot/"
pkgbuild \
  --root "$root/dist/pkgroot" \
  --identifier org.ipad-tech-portal \
  --version "$VERSION" \
  --install-location /Applications \
  "$PKG"

SIGNED_PKG=0
if [[ "$SIGNED_APP" == "1" && -n "${MACOS_CERTIFICATE_INSTALLER_P12:-}" ]]; then
  echo "Signing the pkg with the Developer ID Installer certificate."
  echo "$MACOS_CERTIFICATE_INSTALLER_P12" | base64 -D > "$WORKDIR/installer.p12"
  security import "$WORKDIR/installer.p12" -k "$KEYCHAIN" -P "${MACOS_CERTIFICATE_INSTALLER_PASSWORD:-}" -T /usr/bin/productsign -T /usr/bin/security
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASS" "$KEYCHAIN" >/dev/null
  INSTALLER_IDENTITY="$(security find-identity -v "$KEYCHAIN" | sed -n 's/.*"\(Developer ID Installer:[^"]*\)".*/\1/p' | head -n 1)"
  if [[ -z "$INSTALLER_IDENTITY" ]]; then
    echo "The installer certificate did not yield a Developer ID Installer identity." >&2
    exit 1
  fi
  productsign --sign "$INSTALLER_IDENTITY" "$PKG" "$WORKDIR/signed.pkg"
  mv "$WORKDIR/signed.pkg" "$PKG"
  SIGNED_PKG=1
elif [[ "$SIGNED_APP" == "1" ]]; then
  echo "MACOS_CERTIFICATE_INSTALLER_P12 is not set. The app is signed and the pkg is unsigned."
fi

cp -R "$APP" "$root/dist/dmg-stage/"
rm -f "$DMG"
hdiutil create -volname "iPad Tech Portal" -srcfolder "$root/dist/dmg-stage" -ov -format UDZO "$DMG"
if [[ "$SIGNED_APP" == "1" ]]; then
  codesign --force --timestamp --sign "$APP_IDENTITY" "$DMG"
fi

notary_key="${MACOS_NOTARY_KEY:-}"
notary_key_id="${MACOS_NOTARY_KEY_ID:-}"
notary_issuer="${MACOS_NOTARY_ISSUER_ID:-}"
if [[ -n "$notary_key" || -n "$notary_key_id" || -n "$notary_issuer" ]]; then
  if [[ -z "$notary_key" || -z "$notary_key_id" || -z "$notary_issuer" ]]; then
    echo "Notary secrets are incomplete. Set MACOS_NOTARY_KEY, MACOS_NOTARY_KEY_ID, and MACOS_NOTARY_ISSUER_ID together." >&2
    exit 1
  fi
fi
if [[ -n "$notary_key" && -n "$notary_key_id" && -n "$notary_issuer" ]]; then
  if [[ "$SIGNED_APP" != "1" ]]; then
    echo "Notary credentials are set but the app is unsigned. Skipping notarization."
  else
    echo "$notary_key" | base64 -D > "$WORKDIR/AuthKey.p8"
    xcrun notarytool submit "$DMG" --key "$WORKDIR/AuthKey.p8" --key-id "$notary_key_id" --issuer "$notary_issuer" --wait
    xcrun stapler staple "$DMG"
    if [[ "$SIGNED_PKG" == "1" ]]; then
      xcrun notarytool submit "$PKG" --key "$WORKDIR/AuthKey.p8" --key-id "$notary_key_id" --issuer "$notary_issuer" --wait
      xcrun stapler staple "$PKG"
    fi
  fi
fi

rm -rf "$root/dist/pkgroot" "$root/dist/dmg-stage"
echo "Package: $PKG"
echo "Disk image: $DMG"
