#!/usr/bin/env bash

set -euo pipefail

: "${APPLE_BUILD_NUMBER:?set APPLE_BUILD_NUMBER to the positive CircleCI pipeline number}"
[[ "$APPLE_BUILD_NUMBER" =~ ^[1-9][0-9]*$ ]]

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repository_root"

testflight_dir="build/testflight"
archive_path="$testflight_dir/Veetbot-macOS.xcarchive"
pkg_path="$testflight_dir/Veetbot.pkg"
mkdir -p "$testflight_dir"

echo "Locating CircleCI's ephemeral signing keychain."
signing_keychain="$(
  security list-keychains -d user \
    | tr -d '"' \
    | awk '/\/circleci-signing\.keychain-db$/ {
        sub(/^[[:space:]]+/, ""); print
      }'
)"
[[ "$signing_keychain" == "$HOME/Library/Keychains/circleci-signing.keychain-db" ]]
test -f "$signing_keychain"

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
transferred_identities="$packaging_root/identities.p12"
packaging_keychain="$packaging_root/veetbot-productbuild.keychain-db"
transfer_password="$(uuidgen)$(uuidgen)"
packaging_keychain_password="$(uuidgen)$(uuidgen)"
cleanup_packaging_keychain() {
  security delete-keychain "$packaging_keychain" 2>/dev/null || true
  rm -f -- "$transferred_identities" "$packaging_keychain"
  rmdir "$packaging_root" 2>/dev/null || true
}
trap cleanup_packaging_keychain EXIT
umask 077

export_identities_with_deadline() {
  security export \
    -k "$signing_keychain" \
    -t identities -f pkcs12 \
    -P "$transfer_password" \
    -o "$transferred_identities" &
  local export_pid=$!

  local attempt
  for attempt in {1..12}; do
    if ! kill -0 "$export_pid" 2>/dev/null; then
      wait "$export_pid"
      return
    fi
    echo "Waiting for managed signing identity export (${attempt}/12)."
    sleep 5
  done

  if kill -0 "$export_pid" 2>/dev/null; then
    kill "$export_pid" 2>/dev/null || true
    wait "$export_pid" 2>/dev/null || true
    echo "Managed signing identity export required interaction after 60 seconds." >&2
    return 1
  fi
  wait "$export_pid"
}

echo "Transferring the managed identities into an isolated packaging keychain."
export_identities_with_deadline
test -s "$transferred_identities"
security create-keychain -p "$packaging_keychain_password" "$packaging_keychain"
security set-keychain-settings -lut 3600 "$packaging_keychain"
security unlock-keychain -p "$packaging_keychain_password" "$packaging_keychain"
security import "$transferred_identities" \
  -k "$packaging_keychain" \
  -P "$transfer_password" \
  -T /usr/bin/codesign \
  -T /usr/bin/productbuild \
  -T /usr/bin/security
security set-key-partition-list -S apple-tool:,apple:,codesign: \
  -s -k "$packaging_keychain_password" "$packaging_keychain"

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
