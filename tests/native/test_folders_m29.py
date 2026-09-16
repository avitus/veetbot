"""Execute the native folder acceptance checks; an unavailable lane never passes a gate.

These checks run the real Swift package tests and the macOS XCTest journeys on
the local Apple toolchain. They accept no saved assertions or source matches,
and the registry treats a skipped platform prerequisite as an unpassed gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration

PACKAGE_CASES = (
    "testConfigureSucceedsAndStaysFlatWhenTheFoldersRouteIsMissing",
    "testAFolderLoadFailureNeverPresentsABanner",
    "testReconcileLoadsFoldersAndProposalsAndGroupsTheHistory",
    "testMovingASessionSendsAnExplicitNullAndKeepsItsRowTimestamp",
    "testAcceptingAProposalFilesItsConversationsAndDecliningRemovesIt",
    "testUnavailableFoldersFlattenToTodaysHistory",
    "testMergedHistoryEntryTakesTheServerFolderID",
    "testSessionViewDecodesFolderIDAbsentNullAndPresent",
)
MAC_JOURNEYS = (
    "testFolderSectionsGroupConversationsAndOlderServersStayFlat",
    "testNewFolderSheetCreatesAFolder",
    "testRenamingAFolderThroughItsMenuUpdatesTheSection",
    "testAcceptingASuggestedFolderFilesTheConversation",
    "testDecliningASuggestedFolderRemovesIt",
)


@pytest.fixture(scope="module")
def apple_environment() -> dict[str, str]:
    if sys.platform != "darwin":
        pytest.skip("Native folder acceptance requires macOS/Xcode; this is not a passing gate.")
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


def test_folder_native_degradation(apple_environment: dict[str, str]) -> None:
    """The sidebar degrades to the flat index and reconciles the server's folders."""

    output = native_command(
        [
            "swift",
            "test",
            "--package-path",
            "clients/apple",
            "--filter",
            "ChatViewModelTests|ConversationFoldersTests|SessionHistoryStoreTests|WireModelsTests",
        ],
        apple_environment,
    )
    for name in PACKAGE_CASES:
        assert f"Test {name}() passed" in output, f"Native test did not report passing: {name}"


def test_folder_native_journeys(apple_environment: dict[str, str], tmp_path: Path) -> None:
    """The macOS journeys file, rename, accept, decline, and stay flat on an older server."""

    bundle = tmp_path / "macos.xcresult"
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
            "platform=macOS",
            "-resultBundlePath",
            str(bundle),
            *[
                f"-only-testing:VeetbotUITests/ConversationNavigationUITests/{case}"
                for case in MAC_JOURNEYS
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
    assert summary["passedTests"] == len(MAC_JOURNEYS), summary
    assert summary["failedTests"] == 0 and summary["skippedTests"] == 0, summary
    assert summary["expectedFailures"] == 0, summary
