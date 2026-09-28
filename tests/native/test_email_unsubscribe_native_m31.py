"""Execute the native unsubscribe acceptance checks; an unavailable lane never passes a gate.

These checks run the real Swift package tests and the XCTest Subscriptions
journeys on the local Apple toolchain and accept no saved assertions or source
matches of their own. The registry treats a skipped platform prerequisite as an
unpassed gate.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration

PACKAGE_CASES = (
    "testRowDecodesWithoutAnyOptionalServerField",
    "testOlderServerOmitsSupportAndTheThreadBlock",
    "testSupportAndTheThreadBlockSurviveRoundTrip",
    "testMechanismWordingNamesTheHostAndTheRecipient",
    "testVolumeReportsTheCountedThreadsAndItsOverflow",
    "testNoClientTypeModelsAnUnsubscribeAddress",
    "testBrowsingCarriesTheFiltersAndFollowsTheCursor",
    "testSelectAllSkipsProtectedAndIneligibleRowsAndStopsAtTwentyFive",
    "testAProtectedSenderCanStillBeSelectedIndividually",
    "testAnUnverifiedRowCannotBeSelectedButKeepAndReportSpamRemain",
    "testConfirmingSendsOneConsentedBatchWithTheArchiveChoice",
    "testRowsAreOptimisticUntilTheDurableOperationSettlesThem",
    "testTheThreadActionOpensTheSameConfirmationWithOneRow",
    "testEachRowOffersOnlyWhatItsStateStillAllows",
    "testSupportIsPerAccountAndAbsentOnAnOlderServer",
    "testEmailModeOffersSubscriptionsOnlyWhereTheServerAdvertisesIt",
    "testTheThreadActionSitsBesideTheSenderOnAnEligibleBulkThread",
    "testTheCensusPushesOnIPhoneAndSitsBesideItsDetailElsewhere",
    "testTheConfirmationNamesEachMechanismTheWarningAndTheArchiveOption",
    "testEachSurfacePresentsOnlyTheConfirmationItOpened",
)
PARAMETERIZED_CASES = (
    "testOperationSurvivesRoundTrip(status:)",
    "testEligibilityRequiresVerifiedEvidenceAndAnActionableState(state:)",
    "testStateWordingDistinguishesEachDurableOutcome(state:described:)",
    "testAnUnsettledOutcomeRestoresTheRowWithAnActionableError(outcome:)",
    "testKeepAndUnkeepReplaceTheRowFromTheServer(kept:)",
    "testReportingSpamSettlesFromItsOwnOperation(spam:)",
)
SUITES = (
    "EmailSubscriptionModelTests",
    "EmailSubscriptionsViewModelTests",
    "EmailSubscriptionViewStructureTests",
)
JOURNEYS = (
    "testSubscriptionsCensusUnsubscribesOneSenderAfterConfirmation",
    "testSubscriptionsSelectAllSkipsProtectedAndUncheckedSenders",
    "testSubscriptionsFailedUnsubscribeRestoresTheRowWithWhatRemains",
    "testSubscriptionsThreadActionUnsubscribesTheConversationsSender",
    "testSubscriptionsStayHiddenWhereNoAccountAdvertisesThem",
)


@pytest.fixture(scope="module")
def apple_environment() -> dict[str, str]:
    if sys.platform != "darwin":
        pytest.skip(
            "Native unsubscribe acceptance requires macOS/Xcode; this is not a passing gate."
        )
    developer = Path(os.environ.get("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer"))
    if not (developer / "usr/bin/xcodebuild").is_file():
        pytest.skip("Full Xcode is unavailable; native acceptance has not been verified.")
    return {**os.environ, "DEVELOPER_DIR": str(developer)}


def native_command(arguments: list[str], environment: dict[str, str], *, timeout: int = 600) -> str:
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


def test_unsubscribe_native_experience(apple_environment: dict[str, str]) -> None:
    """The census, its confirmation, the thread action, and honest degradation."""

    output = native_command(
        ["swift", "test", "--package-path", "clients/apple", "--filter", "|".join(SUITES)],
        apple_environment,
    )
    for name in PACKAGE_CASES:
        assert f"Test {name}() passed" in output, f"Native test did not report passing: {name}"
    for name in PARAMETERIZED_CASES:
        passed = re.search(rf"Test {re.escape(name)} with \d+ test cases? passed", output)
        assert passed is not None, f"Native test did not report passing: {name}"
    for suite in SUITES:
        assert f"Suite {suite} passed" in output, f"Native suite did not report passing: {suite}"


def test_unsubscribe_native_journeys(apple_environment: dict[str, str], tmp_path: Path) -> None:
    """The Subscriptions view, confirmation and thread action on Mac, iPhone and iPad."""

    inventory = json.loads(
        native_command(["xcrun", "simctl", "list", "devices", "available", "-j"], apple_environment)
    )
    devices: dict[str, str] = {}
    for family in ("iPhone", "iPad"):
        candidates = [
            (tuple(int(part) for part in runtime.rsplit("iOS-", 1)[1].split("-")), device["udid"])
            for runtime, values in inventory["devices"].items()
            if "iOS-" in runtime
            for device in values
            if device["name"].startswith(family) and device["isAvailable"]
        ]
        if not candidates:
            pytest.skip(f"No {family} simulator is available; the journeys are unverified.")
        devices[family] = max(candidates)[1]

    for platform, destination in (
        ("macos", "platform=macOS"),
        ("iphone", f"platform=iOS Simulator,id={devices['iPhone']}"),
        ("ipad", f"platform=iOS Simulator,id={devices['iPad']}"),
    ):
        bundle = tmp_path / f"{platform}.xcresult"
        native_command(
            [
                "xcodebuild",
                "test",
                "-quiet",
                "-project",
                "clients/apple/Veetbot.xcodeproj",
                "-scheme",
                "Veetbot",
                "-destination",
                destination,
                "-resultBundlePath",
                str(bundle),
                *[
                    f"-only-testing:VeetbotUITests/ConversationNavigationUITests/{case}"
                    for case in JOURNEYS
                ],
                "CODE_SIGN_STYLE=Manual",
                "CODE_SIGN_IDENTITY=-",
                "CODE_SIGNING_REQUIRED=NO",
                "CODE_SIGN_ENTITLEMENTS=",
                "PROVISIONING_PROFILE_SPECIFIER=",
                "DEVELOPMENT_TEAM=",
            ],
            apple_environment,
            timeout=1200,
        )
        summary = json.loads(
            native_command(
                ["xcrun", "xcresulttool", "get", "test-results", "summary", "--path", str(bundle)],
                apple_environment,
            )
        )
        assert summary["passedTests"] == len(JOURNEYS), summary
        assert summary["failedTests"] == 0 and summary["skippedTests"] == 0, summary
        assert summary["expectedFailures"] == 0, summary
