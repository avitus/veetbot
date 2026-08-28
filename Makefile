PYTHON ?= python

.PHONY: install format lint typecheck test check db-up migrate client-build \
	test-static test-contract test-fast test-integration test-live \
	test-sandbox test-apple test-apple-ui test-deploy sandbox-image \
	production-check \
	docs docs-serve docs-check citations-fix

install:
	uv sync --all-groups

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
	uv run pytest -m "not live"

test-static:
	uv run pytest -m static

test-contract:
	@uv run pytest -m "not static and not integration and not live"; \
	status=$$?; test $$status -eq 0 -o $$status -eq 5

test-fast: test-static test-contract

test-integration:
	@uv run pytest -m "integration and not sandbox"; \
	status=$$?; test $$status -eq 0 -o $$status -eq 5

sandbox-image:
	docker build -f execution/sandbox.Dockerfile -t agent-core-sandbox:dev .

test-sandbox: sandbox-image
	uv run pytest -m sandbox

test-live:
	@RUN_LIVE_MODEL_TESTS=1 uv run pytest -m live; \
	status=$$?; test $$status -eq 0 -o $$status -eq 5

test-apple:
	@apple_developer_dir="$${DEVELOPER_DIR:-$$(xcode-select --print-path)}"; \
	if ! printf '%s' "$$apple_developer_dir" | grep -q '\.app/Contents/Developer$$' \
		&& test -d /Applications/Xcode.app/Contents/Developer; then \
		apple_developer_dir=/Applications/Xcode.app/Contents/Developer; \
	fi; \
	if ! printf '%s' "$$apple_developer_dir" | grep -q '\.app/Contents/Developer$$'; then \
		echo 'test-apple requires a full Xcode installation; Command Line Tools only compiles without executing Swift Testing suites.' >&2; \
		exit 1; \
	fi; \
	DEVELOPER_DIR="$$apple_developer_dir" swift test --package-path clients/apple

test-apple-ui:
	@apple_developer_dir="$${DEVELOPER_DIR:-$$(xcode-select --print-path)}"; \
	if ! printf '%s' "$$apple_developer_dir" | grep -q '\.app/Contents/Developer$$' \
		&& test -d /Applications/Xcode.app/Contents/Developer; then \
		apple_developer_dir=/Applications/Xcode.app/Contents/Developer; \
	fi; \
	if ! printf '%s' "$$apple_developer_dir" | grep -q '\.app/Contents/Developer$$'; then \
		echo 'test-apple-ui requires a full Xcode installation.' >&2; \
		exit 1; \
	fi; \
	DEVELOPER_DIR="$$apple_developer_dir" xcodebuild test -quiet \
		-project clients/apple/Veetbot.xcodeproj \
		-scheme Veetbot \
		-destination 'platform=macOS' \
		-only-testing:VeetbotUITests/ConversationNavigationUITests/testMainWindowSizePersistsAcrossApplicationRestart \
		CODE_SIGN_STYLE=Manual CODE_SIGN_IDENTITY=- CODE_SIGNING_REQUIRED=NO \
		CODE_SIGN_ENTITLEMENTS= PROVISIONING_PROFILE_SPECIFIER= DEVELOPMENT_TEAM= || exit $$?; \
	iphone_device_id=$$(DEVELOPER_DIR="$$apple_developer_dir" xcrun simctl list devices available -j \
		| python3 -c 'import json, sys; devices = json.load(sys.stdin)["devices"]; candidates = [(tuple(map(int, runtime.rsplit("iOS-", 1)[1].split("-"))), device["udid"]) for runtime, values in devices.items() if "iOS-" in runtime for device in values if device["name"].startswith("iPhone")]; print(max(candidates)[1] if candidates else "")'); \
	ipad_device_id=$$(DEVELOPER_DIR="$$apple_developer_dir" xcrun simctl list devices available -j \
		| python3 -c 'import json, sys; devices = json.load(sys.stdin)["devices"]; candidates = [(tuple(map(int, runtime.rsplit("iOS-", 1)[1].split("-"))), device["udid"]) for runtime, values in devices.items() if "iOS-" in runtime for device in values if device["name"].startswith("iPad")]; print(max(candidates)[1] if candidates else "")'); \
	if test -z "$$iphone_device_id" -o -z "$$ipad_device_id"; then \
		echo 'test-apple-ui requires available iPhone and iPad simulator runtimes.' >&2; \
		exit 1; \
	fi; \
	for device_id in "$$iphone_device_id" "$$ipad_device_id"; do \
		DEVELOPER_DIR="$$apple_developer_dir" xcodebuild test -quiet \
			-project clients/apple/Veetbot.xcodeproj \
			-scheme Veetbot \
			-destination "platform=iOS Simulator,id=$$device_id" \
			-only-testing:VeetbotUITests || exit $$?; \
	done

test-deploy:
	deploy/app/release.test.sh
	deploy/app/rollback.test.sh
	deploy/nginx/deploy.test.sh

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

check: lint typecheck test-fast test-deploy docs-check

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
