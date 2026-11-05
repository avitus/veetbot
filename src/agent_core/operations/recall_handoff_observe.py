"""Reference Linux observations for the independently installed release controller."""

import hashlib
import http.client
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import Field

from agent_core.domain.general_recall_release import Digest, ReleaseValue
from agent_core.memory.general_recall_evidence import _read_bounded
from agent_core.memory.recall_handoff import Build, HandoffError, ReleaseID, protected
from agent_core.operations.recall_handoff_controller import CONTROLLER, Clearance, Recovery
from agent_core.operations.recall_release_handoff import APP, Environment


class HostProfile(ReleaseValue):
    """Enroll actual host inputs, including all service environment and credential paths."""

    settings: tuple[Path, ...] = Field(min_length=1)
    overlays: tuple[Path, ...] = Field(min_length=1)
    assets: tuple[Path, ...] = Field(min_length=1)
    units: tuple[str, ...] = Field(min_length=7)
    rollback_release: ReleaseID
    rollback_commit: Build
    recovery_paths: tuple[Path, ...] = Field(min_length=3)
    recovery_images: tuple[str, ...] = Field(min_length=3)
    recovery_inventory_sha256: Digest
    recovery_schema_sha256: Digest
    recovery_guard_receipt_sha256: Digest


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def snapshot(paths: tuple[Path, ...]) -> str:
    """Hash file contents and permissions. Never output contents to the release account."""
    files: dict[str, tuple[object, ...]] = {}
    get_attr = getattr(os, "getxattr", None)
    list_attrs = getattr(os, "listxattr", None)
    if get_attr is None or list_attrs is None:
        raise HandoffError("Linux ACL inspection unavailable")

    def visit(path: Path) -> None:
        st = path.lstat()
        acl = {
            name: get_attr(path, name, follow_symlinks=False).hex()
            for name in list_attrs(path, follow_symlinks=False)
            if name.startswith("system.posix_acl")
        }
        entry = (st.st_uid, st.st_gid, st.st_mode, acl)
        if stat.S_ISDIR(st.st_mode):
            files[str(path)] = entry
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(st.st_mode):
            with path.open("rb") as stream:
                files[str(path)] = (*entry, hashlib.file_digest(stream, "sha256").hexdigest())
        elif stat.S_ISLNK(st.st_mode):
            resolved = path.resolve(strict=True)
            if not any(resolved == root or root in resolved.parents for root in paths):
                raise HandoffError("Enroll the resolved artifact before digesting its symlink")
            files[str(path)] = (*entry, str(path.readlink()), str(resolved))
        else:
            raise HandoffError("Snapshot requires regular files and directories")

    for path in paths:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise HandoffError("Host profile contains an indirect path")
        visit(path)
    return digest(files)


def run(*args: str) -> bytes:
    result = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=40,
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "PGSERVICE": "veetbot_recall_verify",
            "PGSERVICEFILE": "/etc/veetbot/recall/handoff-controller/pg_service.conf",
            "PGOPTIONS": "-c default_transaction_read_only=on",
        },
    )
    return result.stdout


def schema() -> str:
    # Hash raw definitions. Never execute the dump. A fixed restriction token
    # keeps the transport framing deterministic without editing SQL contents.
    raw = run(
        "/usr/bin/pg_dump",
        "--schema-only",
        "--restrict-key=VeetbotRecallFingerprint",
        "--dbname=service=veetbot_recall_verify",
    )
    versions = run(
        "/usr/bin/psql",
        "-XAt",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "SHOW server_version; SELECT version_num FROM public.alembic_version "
        "ORDER BY version_num; SELECT extname,extversion FROM pg_extension ORDER BY extname",
    )
    if not raw.strip() or not versions.strip():
        raise HandoffError("Incomplete schema observation")
    return digest([hashlib.sha256(raw).hexdigest(), versions.hex()])


def environment(profile: HostProfile, release: str) -> Environment:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{7,40}", release):
        raise HandoffError("Invalid release ID")
    root = APP / "releases" / release
    if root.resolve(strict=True) != root:
        raise HandoffError("Indirect release directory")
    lines = _read_bounded(root / ".release.env").splitlines()
    if [v for v in lines if v.startswith(b"VEETBOT_RELEASE_ID=")] != [
        b"VEETBOT_RELEASE_ID=" + release.encode()
    ]:
        raise HandoffError("Release environment identity mismatch")
    configured = [v.hex() for v in lines if not v.startswith(b"VEETBOT_RELEASE_ID=")]
    for name in (".owner-scopes.env", ".release.env"):
        st = (root / name).lstat()
        configured.append(f"{name}:{st.st_uid}:{st.st_gid}:{st.st_mode}")
    permissions = _read_bounded(root / ".owner-scopes.env").hex()
    expected_units = {
        f"veetbot-{role}.service"
        for role in ("api", "worker", "async-worker", "maintenance", "schedule", "call", "surface")
    }
    if not expected_units <= set(profile.units):
        raise HandoffError("Incomplete role inventory")
    roles = {}
    for unit in profile.units:
        if not re.fullmatch(r"veetbot-[a-z-]+\.service", unit):
            raise HandoffError("Invalid role unit")
        roles[unit] = digest(
            [
                run("/usr/bin/systemctl", "cat", unit).hex(),
                run(
                    "/usr/bin/systemctl",
                    "show",
                    unit,
                    "--property=User,Group,SupplementaryGroups,Environment,EnvironmentFiles,"
                    "CapabilityBoundingSet,NoNewPrivileges,ReadWritePaths",
                ).decode(),
            ]
        )
    sql = """
        SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,
               rolreplication, rolbypassrls, rolvaliduntil, rolconfig
          FROM pg_roles ORDER BY rolname;
        SELECT * FROM pg_auth_members ORDER BY roleid, member, grantor;
        SELECT datname, datacl FROM pg_database ORDER BY datname;
        SELECT * FROM pg_db_role_setting ORDER BY setdatabase, setrole;
    """
    grants = run("/usr/bin/psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-c", sql)
    return Environment(
        settings_sha256=digest([snapshot(profile.settings), configured]),
        permissions_sha256=digest([permissions, grants.hex()]),
        schema_sha256=schema(),
        roles_sha256=digest(roles),
        overlays_sha256=snapshot(profile.overlays),
        assets_sha256=snapshot(profile.assets),
    )


def read_record(path: Path) -> dict[str, Any]:
    protected(path.parent, directory=True)
    protected(path)
    record = json.loads(_read_bounded(path))
    if not isinstance(record, dict):
        raise HandoffError("Invalid operator record")
    return record


def github(path: str, query: dict[str, Any] | None = None) -> Any:
    """Read GitHub over a fixed origin. Never follow redirects with credentials."""
    repo = "/repos/avitus/veetbot"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "veetbot-recall-verifier"}
    token = CONTROLLER / "github-read-token"
    if token.exists():
        protected(token)
        if token.stat().st_mode & 0o077:
            raise HandoffError("GitHub credential must be private")
        headers["Authorization"] = "Bearer " + _read_bounded(token).decode().strip()
    client = http.client.HTTPSConnection("api.github.com", timeout=10)
    try:
        if query is None:
            client.request("GET", repo + path, headers=headers)
        else:
            if path != "/graphql":
                raise HandoffError("Unexpected GitHub endpoint")
            client.request(
                "POST",
                path,
                body=json.dumps(query),
                headers=headers | {"Content-Type": "application/json"},
            )
        response = client.getresponse()
        if response.status != 200:
            raise HandoffError("GitHub did not return verification data")
        return json.loads(response.read(2**20))
    finally:
        client.close()


def merge_identity(release: str) -> dict[str, str]:
    """Resolve the post-merge release ID independently of staged files."""
    merge = github("/commits/" + release.rsplit("-", 1)[1])
    prs = [
        pr
        for pr in github("/commits/" + merge["sha"] + "/pulls")
        if pr["merge_commit_sha"] == merge["sha"]
        and pr["merged_at"]
        and pr["base"]["ref"] == "main"
        and pr["base"]["repo"]["full_name"] == "avitus/veetbot"
    ]
    if len(prs) != 1:
        raise HandoffError("No unique merged main PR for this release")
    head = github("/commits/" + prs[0]["head"]["sha"])
    return {
        "release": release,
        "merge": merge["sha"],
        "pr": str(prs[0]["number"]),
        "author_id": str(prs[0]["user"]["id"]),
        "merged_at": prs[0]["merged_at"],
        "merge_tree": merge["commit"]["tree"]["sha"],
        "reviewed_head": head["sha"],
        "reviewed_tree": head["commit"]["tree"]["sha"],
    }


def clearance(release: str) -> Clearance:
    """Consume the release owner's existing review/CI receipts, never re-request either."""
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{7,40}", release):
        raise HandoffError("Invalid release")
    target = merge_identity(release)
    head = target["reviewed_head"]
    if not re.fullmatch(r"[a-f0-9]{40}", head):
        raise HandoffError("Invalid reviewed revision")
    directory = CONTROLLER / "reviewed-heads" / head
    if not directory.exists():
        from agent_core.operations.recall_handoff_approval import Gates, resolve_clearance

        gates = Gates.model_validate(read_record(CONTROLLER / "github-gates.json"))
        return resolve_clearance(target, gates, github)
    protected(directory.parent, directory=True)
    review = read_record(directory / "review-clearance.json")
    ci = read_record(directory / "hosted-ci-evidence.json")
    if not (
        target["release"] == release
        and target["merge"].startswith(release.rsplit("-", 1)[1])
        and target["reviewed_head"] == review["final_head"] == ci["head"]
        and target["merge_tree"] == target["reviewed_tree"] == ci["reviewed_tree"]
        and review["all_review_forms_inspected"] is True
        and review["all_threads_resolved"] is True
        and review["remaining_actionable_findings"] == 0
        and ci["all_success"] is True
    ):
        raise HandoffError("Incomplete final-head review, CI or merge evidence")
    return Clearance(
        target_release=release,
        target_commit=target["merge"],
        reviewed_head=ci["head"],
        reviewed_tree=ci["reviewed_tree"],
        merge_tree=target["merge_tree"],
        final_head_review_clear=True,
        all_review_forms_disposed=True,
        final_head_hosted_ci_passed=True,
        review_receipt_sha256=hashlib.sha256(
            _read_bounded(directory / "review-clearance.json")
        ).hexdigest(),
        ci_receipt_sha256=hashlib.sha256(
            _read_bounded(directory / "hosted-ci-evidence.json")
        ).hexdigest(),
    )


def recovery(profile: HostProfile, commit: str) -> Recovery:
    # Reuse the independently recorded interpreter guard only for byte-identical
    # recovery inputs, artifacts, image IDs and a freshly captured identical schema.
    if profile.rollback_commit != commit or not commit.startswith(
        profile.rollback_release.rsplit("-", 1)[1]
    ):
        raise HandoffError("Wrong recovery build")
    images = [
        run("/usr/bin/docker", "image", "inspect", "--format", "{{.Id}}", tag).decode().strip()
        for tag in profile.recovery_images
    ]
    current = digest([snapshot(profile.recovery_paths), images])
    actual_schema = schema()
    if (
        current != profile.recovery_inventory_sha256
        or actual_schema != profile.recovery_schema_sha256
    ):
        raise HandoffError("Recovery inputs no longer match the schema guard receipt")
    return Recovery(
        build_ref=commit,
        schema_sha256=actual_schema,
        assets_and_surfaces_verified=True,
        schema_guard_passed=True,
        verification_sha256=profile.recovery_guard_receipt_sha256,
    )


def main(name: str, value: str) -> None:
    protected(CONTROLLER, directory=True)
    if name == "release-clearance":
        print(clearance(value).model_dump_json())
        return
    profile = HostProfile.model_validate(read_record(CONTROLLER / "host-profile.json"))
    if name == "environment":
        print(environment(profile, value).model_dump_json())
    elif name == "recovery":
        print(recovery(profile, value).model_dump_json())
    else:
        raise HandoffError("Unknown probe")


if __name__ == "__main__":
    main(*sys.argv[1:])
