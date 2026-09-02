#!/usr/bin/env bash

set -euo pipefail

: "${APPLE_BUILD_NUMBER:?set APPLE_BUILD_NUMBER to the positive CircleCI pipeline number}"
: "${APPLE_INSTALLER_CERTIFICATE_BASE64:?set APPLE_INSTALLER_CERTIFICATE_BASE64 in the veetbot-apple-signing context}"
: "${APPLE_INSTALLER_CERTIFICATE_PASSWORD:?set APPLE_INSTALLER_CERTIFICATE_PASSWORD in the veetbot-apple-signing context}"
[[ "$APPLE_BUILD_NUMBER" =~ ^[1-9][0-9]*$ ]]

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repository_root"

testflight_dir="build/testflight"
archive_path="$testflight_dir/Veetbot-macOS.xcarchive"
pkg_path="$testflight_dir/Veetbot.pkg"
mkdir -p "$testflight_dir"

echo "Archiving macOS build $APPLE_BUILD_NUMBER with App Store distribution signing."
xcodebuild archive \
  -project clients/apple/Veetbot.xcodeproj \
  -scheme Veetbot \
  -configuration Release \
  -destination "generic/platform=macOS" \
  -archivePath "$archive_path" \
  CURRENT_PROJECT_VERSION="$APPLE_BUILD_NUMBER" \
  CODE_SIGN_STYLE=Manual \
  CODE_SIGN_IDENTITY="Apple Distribution" \
  PROVISIONING_PROFILE_SPECIFIER="Veetbot Mac App Store"

app_path="$archive_path/Products/Applications/Veetbot.app"
app_info="$app_path/Contents/Info.plist"
archived_build_number="$(
  /usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$app_info"
)"
archived_bundle_id="$(
  /usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app_info"
)"
test "$archived_build_number" = "$APPLE_BUILD_NUMBER"
test "$archived_bundle_id" = "com.veetbot.apple"
codesign --verify --deep --strict --verbose=2 "$app_path"

temporary_root="${TMPDIR:-/tmp}"
packaging_root="$(mktemp -d "${temporary_root%/}/veetbot-package-signing.XXXXXX")"
installer_certificate="$packaging_root/installer.p12"
packaging_keychain="$packaging_root/veetbot-productbuild.keychain-db"
packaging_keychain_passphrase="$(uuidgen)$(uuidgen)"
cleanup_packaging_keychain() {
  security delete-keychain "$packaging_keychain" 2>/dev/null || true
  rm -f -- "$installer_certificate" "$packaging_keychain"
  rmdir "$packaging_root" 2>/dev/null || true
}
trap cleanup_packaging_keychain EXIT
umask 077

echo "Importing the installer identity into an isolated packaging keychain."
printf '%s' "$APPLE_INSTALLER_CERTIFICATE_BASE64" \
  | base64 --decode > "$installer_certificate"
test -s "$installer_certificate"
security create-keychain -p "$packaging_keychain_passphrase" "$packaging_keychain"
security set-keychain-settings -lut 3600 "$packaging_keychain"
security unlock-keychain -p "$packaging_keychain_passphrase" "$packaging_keychain"
security import "$installer_certificate" \
  -k "$packaging_keychain" \
  -P "$APPLE_INSTALLER_CERTIFICATE_PASSWORD" \
  -T /usr/bin/codesign \
  -T /usr/bin/productbuild \
  -T /usr/bin/security
unset APPLE_INSTALLER_CERTIFICATE_BASE64
unset APPLE_INSTALLER_CERTIFICATE_PASSWORD
security set-key-partition-list -S apple-tool:,apple:,codesign: \
  -s -k "$packaging_keychain_passphrase" "$packaging_keychain"

installer_certificate_sha1="$(
  security find-certificate -a -c "3rd Party Mac Developer Installer" -Z \
    "$packaging_keychain" \
    | awk '/^SHA-1 hash: / {print $3}'
)"
[[ "$installer_certificate_sha1" =~ ^[A-F0-9]{40}$ ]]

echo "Signing the installer package with the installed Mac Installer identity."
productbuild \
  --sign "$installer_certificate_sha1" \
  --keychain "$packaging_keychain" \
  --component "$app_path" /Applications \
  "$pkg_path"
test -s "$pkg_path"

echo "Verifying the installer package signature."
pkgutil --check-signature "$pkg_path"
echo "Signed macOS package is ready at $pkg_path."
