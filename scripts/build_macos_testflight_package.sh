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

echo "Authorizing Apple command-line tools to use the ephemeral signing keys."
security unlock-keychain -p "" "$signing_keychain"
security set-key-partition-list -S apple-tool:,apple:,codesign: \
  -s -k "" "$signing_keychain"

installer_certificate_sha1="$(
  security find-certificate -a -c "3rd Party Mac Developer Installer" -Z \
    "$signing_keychain" \
    | awk '/^SHA-1 hash: / {print $3}'
)"
[[ "$installer_certificate_sha1" =~ ^[A-F0-9]{40}$ ]]

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

echo "Signing the installer package with the installed Mac Installer identity."
productbuild \
  --sign "$installer_certificate_sha1" \
  --keychain "$signing_keychain" \
  --component "$app_path" /Applications \
  "$pkg_path"
test -s "$pkg_path"

echo "Verifying the installer package signature."
pkgutil --check-signature "$pkg_path"
echo "Signed macOS package is ready at $pkg_path."
