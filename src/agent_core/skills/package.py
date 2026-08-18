"""Total validation and deterministic tar.zst construction for skill packages."""

from __future__ import annotations

import hashlib
import io
import os
import re
import tarfile
from pathlib import Path, PurePosixPath

import yaml
import zstandard
from pydantic import ValidationError

from agent_core.context.estimator import canonical_json_bytes
from agent_core.domain.errors import SkillValidationError
from agent_core.domain.skills import (
    SKILL_NAME_PATTERN,
    SkillManifest,
    SkillPackage,
    SkillPackageMember,
    ValidatedSkillPackage,
)
from agent_core.ports.context import TokenEstimator

SEMVER_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
MAX_FILES = 64
MAX_PACKAGE_BYTES = 1_048_576
MAX_BODY_TOKENS = 3_000
MAX_METADATA_TOKENS = 75


def _refuse(rule: str, message: str) -> SkillValidationError:
    return SkillValidationError(rule, f"skill package rejected by {rule}: {message}")


def _safe_member_path(raw: str) -> PurePosixPath:
    if "\\" in raw or not raw or raw.startswith("/"):
        raise _refuse("package.path", "member path is not relative POSIX syntax")
    parsed = PurePosixPath(raw)
    if any(part in {"", ".", ".."} for part in parsed.parts):
        raise _refuse("package.path", "member path escapes or aliases the package root")
    return parsed


def _valid_semver(version: str) -> bool:
    if SEMVER_PATTERN.fullmatch(version) is None:
        return False
    without_build = version.split("+", 1)[0]
    _core, separator, prerelease = without_build.partition("-")
    if not separator:
        return True
    return all(
        not (identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"))
        for identifier in prerelease.split(".")
    )


def _parse_skill_markdown(raw: bytes) -> tuple[SkillManifest, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _refuse("manifest.encoding", "SKILL.md must be UTF-8") from exc
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise _refuse("manifest.front_matter", "SKILL.md must begin with YAML front matter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise _refuse("manifest.front_matter", "SKILL.md front matter is not closed") from exc
    try:
        loaded = yaml.safe_load("\n".join(lines[1:closing]))
    except yaml.YAMLError as exc:
        raise _refuse("manifest.yaml", "front matter is invalid YAML") from exc
    if not isinstance(loaded, dict):
        raise _refuse("manifest.fields", "front matter must be a mapping")
    try:
        manifest = SkillManifest.model_validate(loaded)
    except ValidationError as exc:
        raise _refuse(
            "manifest.fields", "front matter has missing, extra, or invalid fields"
        ) from exc
    body = "\n".join(lines[closing + 1 :]).strip()
    if not body:
        raise _refuse("body.length", "instruction body must not be empty")
    return manifest, body


def _canonical_archive(members: tuple[SkillPackageMember, ...]) -> bytes:
    tar_buffer = io.BytesIO()
    normalized = tuple((str(_safe_member_path(member.path)), member.data) for member in members)
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path, data in sorted(normalized, key=lambda item: item[0]):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return zstandard.ZstdCompressor(level=10, threads=0, write_checksum=True).compress(
        tar_buffer.getvalue()
    )


class SkillPackageValidator:
    def __init__(self, estimator: TokenEstimator) -> None:
        self._estimator = estimator

    def _text_tokens(self, text: str) -> int:
        return self._estimator.estimate_text(text, "skill-validation")

    def validate(self, package: SkillPackage) -> ValidatedSkillPackage:
        if SKILL_NAME_PATTERN.fullmatch(package.directory_name) is None:
            raise _refuse("name.grammar", "directory name is invalid")
        if not package.members or len(package.members) > MAX_FILES:
            raise _refuse("package.file_count", f"package must contain 1 to {MAX_FILES} files")
        total_source_bytes = sum(len(member.data) for member in package.members)
        if total_source_bytes > MAX_PACKAGE_BYTES:
            raise _refuse("package.bytes", "package exceeds 1 MiB")
        seen: set[str] = set()
        skill_markdown: bytes | None = None
        for member in package.members:
            parsed = _safe_member_path(member.path)
            normalized = str(parsed)
            if normalized in seen:
                raise _refuse("package.duplicate", f"duplicate member {normalized!r}")
            seen.add(normalized)
            if member.kind == "symlink":
                raise _refuse("package.symlink", f"symlink {normalized!r} is forbidden")
            if normalized == "SKILL.md":
                skill_markdown = member.data
        if skill_markdown is None:
            raise _refuse("package.required", "SKILL.md is required at the package root")
        manifest, body = _parse_skill_markdown(skill_markdown)
        if manifest.name != package.directory_name:
            raise _refuse("name.directory", "manifest name must equal the directory name")
        if not _valid_semver(manifest.version):
            raise _refuse("version.semver", "version must be valid semantic version syntax")
        if not 1 <= len(manifest.description) <= 500 or "\n" in manifest.description:
            raise _refuse(
                "description.length", "description must be 1 to 500 characters on one line"
            )
        if len(manifest.required_tools) > 10 or len(set(manifest.required_tools)) != len(
            manifest.required_tools
        ):
            raise _refuse(
                "required_tools.count", "required_tools must contain 0 to 10 unique names"
            )
        if any(TOOL_NAME_PATTERN.fullmatch(name) is None for name in manifest.required_tools):
            raise _refuse("required_tools.name", "required_tools contains an invalid tool name")
        body_tokens = self._text_tokens(body)
        if body_tokens > MAX_BODY_TOKENS:
            raise _refuse(
                "body.tokens",
                f"body measures {body_tokens} tokens; maximum is {MAX_BODY_TOKENS}",
            )
        metadata_tokens = self._text_tokens(
            canonical_json_bytes(manifest.model_dump(mode="json")).decode("utf-8")
        )
        if metadata_tokens > MAX_METADATA_TOKENS:
            raise _refuse(
                "metadata.tokens",
                f"metadata measures {metadata_tokens} tokens; maximum is {MAX_METADATA_TOKENS}",
            )
        archive = _canonical_archive(package.members)
        if len(archive) > MAX_PACKAGE_BYTES:
            raise _refuse("package.archive_bytes", "canonical archive exceeds 1 MiB")
        return ValidatedSkillPackage(
            manifest=manifest,
            body=body,
            body_tokens=body_tokens,
            content_sha256=hashlib.sha256(archive).hexdigest(),
            archive=archive,
            package_bytes=len(archive),
            file_count=len(package.members),
        )


def package_from_directory(root: Path) -> SkillPackage:
    """Read one package without following symlinks or escaping its directory."""

    if root.is_symlink() or not root.is_dir():
        raise _refuse("package.directory", "package root must be a directory")
    members: list[SkillPackageMember] = []
    total_bytes = 0
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in [*names, *files]:
            candidate = directory_path / name
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                if len(members) >= MAX_FILES:
                    raise _refuse(
                        "package.file_count", f"package must contain 1 to {MAX_FILES} files"
                    )
                members.append(SkillPackageMember(path=relative, kind="symlink"))
            elif candidate.is_file():
                if len(members) >= MAX_FILES:
                    raise _refuse(
                        "package.file_count", f"package must contain 1 to {MAX_FILES} files"
                    )
                remaining = MAX_PACKAGE_BYTES - total_bytes
                with candidate.open("rb") as source:
                    data = source.read(remaining + 1)
                if len(data) > remaining:
                    raise _refuse("package.bytes", "package exceeds 1 MiB")
                total_bytes += len(data)
                members.append(SkillPackageMember(path=relative, data=data))
    return SkillPackage(directory_name=root.name, members=tuple(members))


def _decompress_archive(archive_bytes: bytes) -> bytes:
    try:
        content_size = zstandard.frame_content_size(archive_bytes)
        if content_size == zstandard.CONTENTSIZE_ERROR:
            raise _refuse("package.corrupt", "package archive is not valid zstd")
        if content_size != zstandard.CONTENTSIZE_UNKNOWN and content_size > MAX_PACKAGE_BYTES * 4:
            raise _refuse("package.archive_bytes", "expanded archive exceeds 4 MiB")
        return zstandard.ZstdDecompressor().decompress(
            archive_bytes,
            max_output_size=MAX_PACKAGE_BYTES * 4,
        )
    except zstandard.ZstdError as exc:
        raise _refuse("package.corrupt", "package archive is not valid zstd") from exc


def read_archive_member(archive_bytes: bytes, path: str) -> bytes:
    requested = str(_safe_member_path(path))
    tar_bytes = _decompress_archive(archive_bytes)
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
            member = archive.getmember(requested)
            if not member.isfile():
                raise _refuse("package.member", "requested member is not a regular file")
            opened = archive.extractfile(member)
            if opened is None:
                raise _refuse("package.member", "requested member could not be opened")
            payload = opened.read(MAX_PACKAGE_BYTES + 1)
            if len(payload) > MAX_PACKAGE_BYTES:
                raise _refuse("package.member_bytes", "requested member exceeds 1 MiB")
            return payload
    except (KeyError, tarfile.TarError) as exc:
        raise _refuse("package.member", f"member {requested!r} was not found") from exc


def read_archive_members(archive_bytes: bytes) -> tuple[SkillPackageMember, ...]:
    """Read every regular member from a validated canonical skill archive."""

    tar_bytes = _decompress_archive(archive_bytes)
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
            members: list[SkillPackageMember] = []
            total_bytes = 0
            for member in archive.getmembers():
                path = str(_safe_member_path(member.name))
                if not member.isfile():
                    raise _refuse("package.member", "archive contains a non-regular member")
                opened = archive.extractfile(member)
                if opened is None:
                    raise _refuse("package.member", "archive member could not be opened")
                data = opened.read(MAX_PACKAGE_BYTES + 1)
                total_bytes += len(data)
                if total_bytes > MAX_PACKAGE_BYTES:
                    raise _refuse("package.member_bytes", "archive members exceed 1 MiB")
                members.append(SkillPackageMember(path=path, data=data))
            return tuple(members)
    except tarfile.TarError as exc:
        raise _refuse("package.corrupt", "package archive is not valid tar") from exc
