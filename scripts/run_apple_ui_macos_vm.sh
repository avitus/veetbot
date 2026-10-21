#!/bin/sh
# Run `make test-apple-ui-macos` inside a local Tart macOS VM, or any other
# command given as arguments (such as one `xcodebuild test -only-testing:`
# case) from the guest's copy of the checkout.
#
# The macOS UI cases synthesize real pointer and keyboard events, so on the
# host they take over the owner's desktop for the whole lane. Inside the VM
# they drive the guest's own window server instead. The VM boots for each run
# with the checkout shared read-only, copies it to the guest disk (where the
# guest's DerivedData stays warm between runs), runs the unchanged make target,
# writes the result bundle back through a second share, and shuts down. One run
# holds the VM at a time; a second waits for it.
set -eu

vm=${APPLE_UI_VM:-veetbot-macos-ui}
image=${APPLE_UI_VM_IMAGE:-ghcr.io/cirruslabs/macos-tahoe-xcode:27}
cpu=${APPLE_UI_VM_CPU:-8}
memory=${APPLE_UI_VM_MEMORY:-16384}
display=${APPLE_UI_VM_DISPLAY:-1728x1117}
repo=$(cd "$(dirname "$0")/.." && pwd)
results_root=${APPLE_TEST_RESULTS_DIR:-${TMPDIR:-/tmp}/veetbot-apple-test-results}
tart_home=${TART_HOME:-$HOME/.tart}
lock_dir="$tart_home/$vm.veetbot-lock"
shares="/Volumes/My Shared Files"

if ! command -v tart >/dev/null 2>&1; then
	echo "$0 requires Tart (https://tart.run); see docs/plan/development-toolchain.md." >&2
	exit 1
fi
if ! tart list --format json | python3 -c 'import json, sys; sys.exit(0 if any(vm["Name"] == sys.argv[1] for vm in json.load(sys.stdin)) else 1)' "$vm"; then
	echo "Tart VM '$vm' does not exist. Create it once with:" >&2
	echo "  tart clone $image $vm" >&2
	exit 1
fi

mkdir -p "$tart_home"
waited=""
until mkdir "$lock_dir" 2>/dev/null; do
	holder=$(cat "$lock_dir/pid" 2>/dev/null || true)
	if test -n "$holder" && ! kill -0 "$holder" 2>/dev/null; then
		rm -rf -- "$lock_dir"
		continue
	fi
	if test -z "$waited"; then
		echo "Waiting for the run holding Tart VM '$vm' (pid ${holder:-unknown})." >&2
		waited=1
	fi
	sleep 5
done
echo $$ >"$lock_dir/pid"

vm_pid=""
finish() {
	status=$?
	trap - EXIT INT TERM
	if test -n "$vm_pid"; then
		tart stop "$vm" >/dev/null 2>&1 || true
		wait "$vm_pid" 2>/dev/null || true
	fi
	rm -rf -- "$lock_dir"
	exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

vm_state() {
	tart list --format json | python3 -c 'import json, sys; print(next(vm["State"] for vm in json.load(sys.stdin) if vm["Name"] == sys.argv[1]))' "$vm"
}

# A VM still running under the lock was left by a run that died; its shares
# point at another checkout, so it restarts with this one.
if test "$(vm_state)" = running; then
	tart stop "$vm" >/dev/null
fi
tart set "$vm" --cpu "$cpu" --memory "$memory" --display "${display}pt"

results_dir="$results_root/vm-run-$(date -u +%Y%m%d-%H%M%S)-$$"
mkdir -p "$results_dir"
vm_log="$results_dir/tart-run.log"
tart run --no-graphics --dir="src:$repo:ro" --dir="results:$results_dir" "$vm" >"$vm_log" 2>&1 &
vm_pid=$!

attempt=0
until tart exec "$vm" test -d "$shares/src" >/dev/null 2>&1; do
	attempt=$((attempt + 1))
	if test "$attempt" -ge 90 || ! kill -0 "$vm_pid" 2>/dev/null; then
		echo "Tart VM '$vm' did not come up with its shares; see $vm_log." >&2
		exit 1
	fi
	sleep 2
done

# Guest UI tests need automation mode without an authentication prompt, which
# no one can answer in a headless guest. The setting persists on the VM disk.
if ! tart exec "$vm" automationmodetool | grep -q 'DOES NOT REQUIRE'; then
	tart exec "$vm" sudo -n automationmodetool enable-automationmode-without-authentication
fi

# The cases size windows against a laptop-sized screen; see the Swift file.
tart exec "$vm" swift "$shares/src/scripts/apple_ui_vm_display.swift" "${display%x*}" "${display#*x}"

tart exec "$vm" /bin/sh -c "mkdir -p ~/veetbot && rsync -a --delete \
	--exclude .git --exclude .venv --exclude node_modules --exclude .build \
	--exclude site --exclude dist --exclude build --exclude .chunk \
	'$shares/src/' ~/veetbot/"

if test "$#" -eq 0; then
	set -- make test-apple-ui-macos
fi
echo "Running '$*' in Tart VM '$vm'; results go to $results_dir." >&2
tart exec "$vm" /usr/bin/env APPLE_TEST_RESULTS_DIR="$shares/results" \
	/bin/zsh -lc 'cd ~/veetbot && exec "$@"' zsh "$@"
