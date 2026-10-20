PYTHON ?= python
CHECK_JOBS ?= 2
STATIC_TEST_WORKERS ?= 2
# Extra pytest arguments for the static, contract and integration partitions:
# CircleCI Smarter Testing passes the test files it selects and its report
# path here (.circleci/test-suites.yml). Empty runs the whole partition.
PYTEST_ARGS ?=
BROWSER_IMAGE ?= veetbot-browser-profile-service:check
HOST_OS ?= $(shell uname -s)
# The simulator UI suite runs on these devices; CI gives each its own executor.
IOS_UI_DEVICES ?= iphone ipad
# INDEX/TOTAL runs every TOTAL-th enumerated case from INDEX (ADR-0230).
IOS_UI_SHARD ?=

# Keep one Make graph so repeated goals share prerequisites. Independent lanes
# overlap; static and contract tests retain their specified sequence.
ifneq ($(filter check,$(MAKECMDGOALS)),)
MAKEFLAGS += -j$(CHECK_JOBS)
endif
ifneq ($(filter check test-fast,$(MAKECMDGOALS)),)
test-contract: | test-static
endif

.PHONY: install format lint typecheck test check db-up migrate env-pull client-build \
	test-static test-contract test-fast test-integration test-live browser-install test-browser \
	test-browser-cases \
	test-sandbox test-apple test-apple-ui test-apple-ui-macos test-apple-ui-macos-vm \
	test-apple-ui-ios \
	test-deploy sandbox-image browser-image test-browser-image \
	production-check \
	docs docs-serve docs-check citations-fix website-install test-website

install:
	uv sync --all-groups

browser-install:
	uv run playwright install --with-deps chromium

# Doppler holds the development secrets; .env is a generated cache of them.
# Regenerate it after changing a secret, and in each new worktree. install
# deliberately does not depend on this: a fresh clone and the chunk sidecar
# have no Doppler credentials and use the .env.example path instead.
env-pull:
	@command -v doppler >/dev/null 2>&1 || { \
		echo "env-pull needs the doppler CLI: brew install dopplerhq/cli/doppler" >&2; \
		exit 1; \
	}
	@tmp=$$(mktemp "$(CURDIR)/.env.tmp.XXXXXX") && \
	chmod 600 "$$tmp" && \
	if doppler secrets download --format=env --no-file --no-fallback >"$$tmp"; then \
		mv -f "$$tmp" "$(CURDIR)/.env"; \
		echo "wrote $(CURDIR)/.env from doppler"; \
	else \
		rm -f "$$tmp"; \
		exit 1; \
	fi

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy src client tests scripts/build_client.py

client-build:
	uv run $(PYTHON) scripts/build_client.py

test:
	uv run pytest -m "not live and not browser_image"

test-static:
	uv run pytest -n $(STATIC_TEST_WORKERS) --dist loadscope -m static $(PYTEST_ARGS)

test-contract:
	@uv run pytest -m "not static and not integration and not live and not browser" \
		$(PYTEST_ARGS); \
	status=$$?; test $$status -eq 0 -o $$status -eq 5

test-fast: test-static test-contract

# Hosted browsers are headed. Linux gives each a private Xvfb display; a Mac
# would open the windows on the owner's desktop, so there both browser targets
# run on the Linux chunk sidecar instead (ADR-0229). VEETBOT_BROWSER_ON_HOST=1
# runs them on the Mac deliberately. test-browser-cases runs only the named
# cases, so it writes no gate report.
ifeq ($(HOST_OS)$(VEETBOT_BROWSER_ON_HOST),Darwin)
test-browser:
	@scripts/run_on_sidecar.sh make test-browser

test-browser-cases:
	@scripts/run_on_sidecar.sh "make test-browser-cases CASES='$(CASES)'"
else
test-browser: browser-install
	VEETBOT_REQUIRE_REAL_BROWSER=1 uv run pytest -n 0 -m browser \
		-p tests.browser_report --browser-report test-results/browser/baseline.json

test-browser-cases: browser-install
	@test -n "$(CASES)" || { echo 'test-browser-cases needs CASES=<pytest paths or node ids>' >&2; exit 2; }
	VEETBOT_REQUIRE_REAL_BROWSER=1 uv run pytest -n 0 $(CASES)
endif

test-integration:
	@uv run pytest -m "integration and not sandbox and not browser_image" $(PYTEST_ARGS); \
	status=$$?; test $$status -eq 0 -o $$status -eq 5

sandbox-image:
	docker build -f execution/sandbox.Dockerfile -t agent-core-sandbox:dev .

test-sandbox: sandbox-image
	uv run pytest -m sandbox

# The browser's own requests, measured in the service image as built (ADR-0152).
browser-image:
	docker build -f deploy/browser-profile-service.Dockerfile -t $(BROWSER_IMAGE) .

test-browser-image: browser-image
	VEETBOT_BROWSER_IMAGE=$(BROWSER_IMAGE) uv run pytest -m browser_image

test-live:
	@RUN_LIVE_MODEL_TESTS=1 uv run pytest -m live; \
	status=$$?; test $$status -eq 0 -o $$status -eq 5

# Native targets need full Xcode: Command Line Tools can compile a Swift
# Testing bundle without executing it and cannot run UI tests.
APPLE_FULL_XCODE = apple_developer_dir="$${DEVELOPER_DIR:-$$(xcode-select --print-path)}"; \
	if ! printf '%s' "$$apple_developer_dir" | grep -q '\.app/Contents/Developer$$' \
		&& test -d /Applications/Xcode.app/Contents/Developer; then \
		apple_developer_dir=/Applications/Xcode.app/Contents/Developer; \
	fi; \
	if ! printf '%s' "$$apple_developer_dir" | grep -q '\.app/Contents/Developer$$'; then \
		echo '$@ requires a full Xcode installation; Command Line Tools only compiles without executing tests.' >&2; \
		exit 1; \
	fi
APPLE_RESULTS_RUN_DIR = apple_results_root="$${APPLE_TEST_RESULTS_DIR:-$${TMPDIR:-/tmp}/veetbot-apple-test-results}"; \
	apple_results_run_dir="$$apple_results_root/run-$$(date -u +%Y%m%d-%H%M%S)-$$$$"; \
	mkdir -p "$$apple_results_run_dir"

test-apple:
	@$(APPLE_FULL_XCODE); \
	DEVELOPER_DIR="$$apple_developer_dir" swift test --package-path clients/apple

# Mac UI tests drive the real desktop, so locally they run inside a Tart macOS
# VM and the families never overlap; CI runs each target on its own executor
# at the same time.
test-apple-ui:
	@$(MAKE) --no-print-directory test-apple-ui-macos-vm
	@$(MAKE) --no-print-directory test-apple-ui-ios

test-apple-ui-macos:
	@$(APPLE_FULL_XCODE); \
	$(APPLE_RESULTS_RUN_DIR); \
	DEVELOPER_DIR="$$apple_developer_dir" xcodebuild test -quiet \
		-project clients/apple/Veetbot.xcodeproj \
		-scheme Veetbot \
		-destination 'platform=macOS' \
		-resultBundlePath "$$apple_results_run_dir/macos.xcresult" \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testMainWindowSizePersistsAcrossApplicationRestart \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testTaskApprovalWebsitesCanBeAddedAndRemovedInSettings \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testChatApprovalCollapsesAfterApproval \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testChatApprovalLoadedApprovedStartsCollapsed \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testBrowserChatApprovalCollapsesAfterTaskApproval \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testPeopleAccessibilityAtLargeText \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testPeopleEvidencePreservesTheOpenConversation \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testMemoryPeopleShowsEachChosenPerson \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSynthesisExplainsSourcesAndOpensAnOriginal \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSynthesisUnavailableOriginalDoesNotReuseItsPreview \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSynthesisReviewAndDeletePreserveOriginals \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSynthesisConnectionLabelsUncertaintyAndEvidence \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSynthesisUndoShowsAffectedOriginalsBeforeCommit \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSynthesisOpaqueHistoryHasNoSourcesOrActions \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSynthesisUnavailableOnOlderServer \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testFinishedAnswerOffersCopyAndSelectText \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSVGArtifactCanBeDownloadedOnMac \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testPeopleIdentityPreviewRemainsReviewableAfterEditorCloses \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testPeopleMailboxImportPreviewShowsExplicitCoverage \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testPeopleForgetExplainsSourceRetentionAndPendingCleanup \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testMixedToolSummaryKeepsAnswerVisibleAndExpandsDetails \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testStreamedAnswerEndsAboveTheComposer \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testLongStreamedAnswerKeepsItsBeginningInView \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testEmailModeAndExactDraftApprovalOnMac \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testEmailCanBeCheckedOffFromInboxWithoutOpeningThread \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testEmailDetailArchiveDisplaysNextConversation \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testEmailRowArchiveOfOpenConversationDisplaysNext \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testEmailArchiveFailurePreservesInboxRow \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testEmailReadingKeepsFeedbackOptionalAndReplyReachable \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testEmailReadingInDarkAppearance \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testEmailReadingAtDefaultMacWindowSize \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testEmailSidebarShowsFivePrioritiesWithoutScrolling \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testEmailSidebarResizesWithItsDivider \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testEmailSidebarWidthPersistsAcrossApplicationRestart \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testEmailFeedbackRequiresAnExplicitPersonAndClearsChangedTargets \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testOlderServerSidebarStaysFlatWithoutFolderControls \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testRecentChatsIncludesFiledChatsAndCollapses \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testRecentChatsWorksWithoutFolders \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testFolderSectionsGroupConversations \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testNewFolderSheetCreatesAFolder \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testRenamingAFolderThroughItsMenuUpdatesTheSection \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testAcceptingASuggestedFolderFilesTheConversation \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testDecliningASuggestedFolderRemovesIt \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testOpeningAFolderInSoloModeClosesTheOpenOne \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testANewProposalLeavesFolderExpansionAlone \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testASuggestedFolderListsEachConversationItWouldFile \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testRenamingASuggestedFolderBeforeAcceptingIt \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testFolderNameSheetIsSizedForItsContentOnMac \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testAdjacentFoldersSitOneRowApartOnMac \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testMovingAConversationIntoAFolderAndBackOut \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSubscriptionsCensusUnsubscribesOneSenderAfterConfirmation \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSubscriptionsSelectAllSkipsProtectedAndUncheckedSenders \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSubscriptionsFailedUnsubscribeRestoresTheRowWithWhatRemains \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSubscriptionsThreadActionUnsubscribesTheConversationsSender \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSubscriptionsStayHiddenWhereNoAccountAdvertisesThem \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSuspectedSpamNotSpamClearsTheFlag \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testSuspectedSpamReportSpamRemovesTheConversation \
		CODE_SIGN_STYLE=Manual CODE_SIGN_IDENTITY=- CODE_SIGNING_REQUIRED=NO \
		CODE_SIGN_ENTITLEMENTS= PROVISIONING_PROFILE_SPECIFIER= DEVELOPMENT_TEAM=

# The same target inside a local Tart macOS VM, so the cases synthesize input
# on the guest's display instead of the owner's.
test-apple-ui-macos-vm:
	@scripts/run_apple_ui_macos_vm.sh

# Xcode 27.0 installs the UI-test runner about four seconds into a boot it
# starts itself. Two cold boots at once bring SpringBoard up after that, it
# holds the runner as still being updated, and the launch is refused as Busy.
# Both devices therefore finish booting before either run starts.
# Each pick is the newest iOS runtime's device, ties broken by udid. The
# iPhone pick skips the foldable iPhone Duo device type, which is the only
# iPhone the iOS 27.1 runtime offers, so a standard iPhone wins.
test-apple-ui-ios:
	@$(APPLE_FULL_XCODE); \
	$(APPLE_RESULTS_RUN_DIR); \
	iphone_device_id=$$(DEVELOPER_DIR="$$apple_developer_dir" xcrun simctl list devices available -j \
		| python3 -c 'import json, sys; devices = json.load(sys.stdin)["devices"]; candidates = [(tuple(map(int, runtime.rsplit("iOS-", 1)[1].split("-"))), device["udid"]) for runtime, values in devices.items() if "iOS-" in runtime for device in values if device["name"].startswith("iPhone") and not device["deviceTypeIdentifier"].rsplit(".", 1)[-1].startswith("iPhone-Duo")]; print(max(candidates)[1] if candidates else "")'); \
	ipad_device_id=$$(DEVELOPER_DIR="$$apple_developer_dir" xcrun simctl list devices available -j \
		| python3 -c 'import json, sys; devices = json.load(sys.stdin)["devices"]; candidates = [(tuple(map(int, runtime.rsplit("iOS-", 1)[1].split("-"))), device["udid"]) for runtime, values in devices.items() if "iOS-" in runtime for device in values if device["name"].startswith("iPad")]; print(max(candidates)[1] if candidates else "")'); \
	run_iphone=; run_ipad=; \
	for device in $(IOS_UI_DEVICES); do \
		case "$$device" in \
			iphone) run_iphone=1 ;; \
			ipad) run_ipad=1 ;; \
			*) echo "IOS_UI_DEVICES names iphone and ipad, not $$device." >&2; exit 2 ;; \
		esac; \
	done; \
	if { test -n "$$run_iphone" && test -z "$$iphone_device_id"; } \
		|| { test -n "$$run_ipad" && test -z "$$ipad_device_id"; }; then \
		echo '$@ requires available iPhone and iPad simulator runtimes.' >&2; \
		exit 1; \
	fi; \
	test -n "$$run_iphone" || iphone_device_id=""; \
	test -n "$$run_ipad" || ipad_device_id=""; \
	apple_ui_tmp=$$(mktemp -d "$${TMPDIR:-/tmp}/veetbot-apple-ui.XXXXXX"); \
	simulators_booted_here=""; \
	finish_apple_ui() { \
		for device_id in $$simulators_booted_here; do \
			DEVELOPER_DIR="$$apple_developer_dir" xcrun simctl shutdown "$$device_id" >/dev/null 2>&1 & \
		done; \
		wait; \
		rm -rf -- "$$apple_ui_tmp"; \
	}; \
	trap finish_apple_ui EXIT; \
	test_products_path="$$apple_ui_tmp/VeetbotUITests.xctestproducts"; \
	DEVELOPER_DIR="$$apple_developer_dir" xcodebuild build-for-testing -quiet \
		-project clients/apple/Veetbot.xcodeproj \
		-scheme Veetbot \
		-destination "platform=iOS Simulator,id=$${iphone_device_id:-$$ipad_device_id}" \
		-testProductsPath "$$test_products_path" \
		-only-testing:VeetbotUITests || exit $$?; \
	boot_ios_simulator() { \
		test -n "$$1" || return 0; \
		boot_report=$$(DEVELOPER_DIR="$$apple_developer_dir" xcrun simctl bootstatus "$$1" -b 2>&1) || { \
			printf '%s\n' "$$boot_report" >&2; \
			return 1; \
		}; \
		if printf '%s\n' "$$boot_report" | grep -q '^[[:space:]]*Finished$$'; then \
			return 0; \
		fi; \
		echo "Simulator $$1 did not report a finished boot; waiting for SpringBoard instead. Erasing the simulator repairs a failed data migration." >&2; \
		attempt=0; \
		while test "$$attempt" -lt 120; do \
			springboard_pid=$$(DEVELOPER_DIR="$$apple_developer_dir" xcrun simctl spawn "$$1" \
				notifyutil -g com.apple.springboard.finishedstartup 2>/dev/null | awk '{print $$NF}'); \
			case "$$springboard_pid" in \
				''|0|*[!0-9]*) ;; \
				*) return 0 ;; \
			esac; \
			attempt=$$((attempt + 1)); \
			sleep 0.5; \
		done; \
		echo "SpringBoard on simulator $$1 did not finish starting." >&2; \
		return 1; \
	}; \
	for device_id in "$$iphone_device_id" "$$ipad_device_id"; do \
		test -n "$$device_id" || continue; \
		if ! DEVELOPER_DIR="$$apple_developer_dir" xcrun simctl list devices booted | grep -q "$$device_id"; then \
			simulators_booted_here="$$simulators_booted_here $$device_id"; \
		fi; \
	done; \
	boot_ios_simulator "$$iphone_device_id" & \
	iphone_boot_pid=$$!; \
	boot_ios_simulator "$$ipad_device_id" & \
	ipad_boot_pid=$$!; \
	wait "$$iphone_boot_pid"; iphone_boot_status=$$?; \
	wait "$$ipad_boot_pid"; ipad_boot_status=$$?; \
	if test "$$iphone_boot_status" -ne 0 -o "$$ipad_boot_status" -ne 0; then \
		echo "Apple simulators failed to boot (iPhone=$$iphone_boot_status, iPad=$$ipad_boot_status)." >&2; \
		exit 1; \
	fi; \
	only_testing=-only-testing:VeetbotUITests; \
	if test -n "$(IOS_UI_SHARD)"; then \
		DEVELOPER_DIR="$$apple_developer_dir" xcodebuild test-without-building -quiet \
			-destination "platform=iOS Simulator,id=$${iphone_device_id:-$$ipad_device_id}" \
			-testProductsPath "$$test_products_path" \
			-collect-test-diagnostics never \
			-enumerate-tests -test-enumeration-format json \
			-test-enumeration-output-path "$$apple_ui_tmp/tests.json" \
			-only-testing:VeetbotUITests || exit $$?; \
		only_testing=$$(python3 scripts/ios_ui_shard.py "$$apple_ui_tmp/tests.json" "$(IOS_UI_SHARD)") || exit $$?; \
	fi; \
	run_ios_ui_tests() { \
		device_label="$$1"; \
		device_id="$$2"; \
		test -n "$$device_id" || return 0; \
		DEVELOPER_DIR="$$apple_developer_dir" xcodebuild test-without-building -quiet \
			-destination "platform=iOS Simulator,id=$$device_id" \
			-testProductsPath "$$test_products_path" \
			-resultBundlePath "$$apple_results_run_dir/$$device_label.xcresult" \
			-collect-test-diagnostics never \
			$$only_testing; \
	}; \
	run_ios_ui_tests iphone "$$iphone_device_id" & \
	iphone_pid=$$!; \
	run_ios_ui_tests ipad "$$ipad_device_id" & \
	ipad_pid=$$!; \
	wait "$$iphone_pid"; iphone_status=$$?; \
	wait "$$ipad_pid"; ipad_status=$$?; \
	if test "$$iphone_status" -ne 0 -o "$$ipad_status" -ne 0; then \
		echo "Apple simulator UI tests failed (iPhone=$$iphone_status, iPad=$$ipad_status)." >&2; \
		exit 1; \
	fi

test-deploy:
	deploy/app/release.test.sh
	deploy/app/rollback.test.sh
	deploy/nginx/deploy.test.sh

website-install:
	node scripts/install_website.mjs

test-website: website-install
	node --test website/tests/install-dependencies.test.mjs
	npm --prefix website test
	npm --prefix website run lint

production-check:
	uv run python scripts/check_production_deployment.py

docs:
	uv run $(PYTHON) scripts/build_docs.py

docs-serve:
	uv run mkdocs serve

docs-check:
	uv run $(PYTHON) scripts/check_docs.py

citations-fix:
	uv run $(PYTHON) scripts/check_citations.py --update

check: test-fast test-browser test-website lint typecheck test-deploy docs-check

db-up:
	docker compose up -d postgres
	@attempt=0; \
	pg_container_id=$$(docker compose ps -q postgres); \
	test -n "$$pg_container_id"; \
	while test "$$(docker inspect --format '{{.State.Health.Status}}' "$$pg_container_id")" != healthy; do \
		attempt=$$((attempt + 1)); \
		if test $$attempt -ge 30; then \
			docker compose ps postgres; \
			exit 1; \
		fi; \
		sleep 2; \
	done

migrate:
	uv run alembic upgrade head
