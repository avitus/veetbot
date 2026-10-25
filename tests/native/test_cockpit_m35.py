"""Execute the native Cockpit acceptance checks; an unavailable lane never passes a gate.

docs/plan/cockpit.md, the native experience. These checks run the real Swift
package tests on the local Apple toolchain and the realm suite in headless
browsers: the realm's lack of network in Chromium, WebKit and a hosted
`WKWebView` (gate 16), web content without a credential (gate 17), the scheme
handler and navigation policy (gate 18), and the board's view model holding
its stream only while Cockpit is visible (gate 21). They accept no saved
assertions or source matches, and the registry treats a skipped platform
prerequisite as an unpassed gate.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration

SCHEME_CASES = (
    "aNamedHashIsFetchedNativelyAndServedWithoutCredentials",
    "anythingElseIs404AndNeverReachesTheServer",
    "aStoppedLoadReceivesNothingAfterItsStop",
    "theSchemeHandlerMayServeOnlyTheSnapshotsOwnHashes",
    "onlyAContentHashUnderTheBoardResolves",
    "navigationAdmitsOnlyTheHostDocumentAndItsRealm",
    "theBridgeAcceptsOnlyTheHostsOwnMessagesFromTheMainFrame",
    "theWebContentLosesItsNetworkPrimitivesAndEveryWebLoad",
)
HOSTED_CASES = (
    "aHostedBoardRendersItsWidgetsAndNoEscapeReachesTheNetwork",
    "aWidgetThatNavigatesIsCutOffAndReported",
)
CREDENTIAL_CASES = (
    "aNamedHashIsFetchedNativelyAndServedWithoutCredentials",
    "aHostedBoardRendersItsWidgetsAndNoEscapeReachesTheNetwork",
)
STREAM_CASES = (
    "becomingVisibleLoadsTheBoardAndStreamsAfterItsCursorUntilHidden",
    "aServerWithoutTheRoutesIsUnavailableAndNothingStreams",
    "everyChangeRefetchesTheBoardAndAResetReopensFromTheNewCursor",
    "aFailingStreamReconnectsWithCappedBackoffAndNeverGivesUp",
)


def _run(arguments: list[str], environment: dict[str, str], *, timeout: int = 900) -> str:
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-16000:]
    return output


@pytest.fixture(scope="module")
def apple_environment() -> dict[str, str]:
    if sys.platform != "darwin":
        pytest.skip("Native Cockpit acceptance requires macOS/Xcode; this is not a passing gate.")
    developer = Path(os.environ.get("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer"))
    if not (developer / "usr/bin/xcodebuild").is_file():
        pytest.skip("Full Xcode is unavailable; native acceptance has not been verified.")
    if not (ROOT / "cockpit/node_modules").is_dir():
        pytest.skip("The cockpit workspace is not installed (make cockpit-install); unverified.")
    return {**os.environ, "DEVELOPER_DIR": str(developer)}


@pytest.fixture(scope="module")
def apple_cockpit(apple_environment: dict[str, str]) -> str:
    # The hosted tests load the real runtime this build exports.
    _run(["npm", "--prefix", "cockpit", "run", "build"], apple_environment)
    return _run(
        [
            "swift",
            "test",
            "--package-path",
            "clients/apple",
            "--filter",
            "CockpitBoardTests|CockpitSchemeHandlerTests|CockpitViewModelTests|CockpitWireTests"
            "|CockpitHostedBoardTests",
        ],
        apple_environment,
    )


def _passed(output: str, name: str) -> None:
    assert f"Test {name}() passed" in output, f"Native test did not report passing: {name}"


def test_cockpit_realm_has_no_network(
    apple_cockpit: str, apple_environment: dict[str, str]
) -> None:
    for name in HOSTED_CASES:
        _passed(apple_cockpit, name)
    realm = _run(
        ["node", "--test", "--test-reporter=tap", "cockpit/tests/realm.test.mjs"], apple_environment
    )
    assert "# fail 0" in realm and "# skipped 0" in realm, realm[-4000:]
    for engine in ("chromium", "webkit"):
        assert f"- {engine}: no member of the adversarial corpus reaches the network" in realm
        assert f"- {engine}: a navigating widget is quarantined" in realm


def test_cockpit_web_content_holds_no_credential(apple_cockpit: str) -> None:
    for name in CREDENTIAL_CASES:
        _passed(apple_cockpit, name)


def test_cockpit_scheme_handler_serves_only_named_assets(apple_cockpit: str) -> None:
    for name in SCHEME_CASES:
        _passed(apple_cockpit, name)


def test_cockpit_streams_only_while_visible(apple_cockpit: str) -> None:
    for name in STREAM_CASES:
        _passed(apple_cockpit, name)
