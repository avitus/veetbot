"""The GitHub App key the maintenance unit loads (docs/deployment.md, Cockpit).

systemd 255 delivers a `LoadCredential` file on a read-only tmpfs as
`root:root` mode 0440 with a POSIX ACL naming only the unit's user, so the
group bits `stat` reports are the ACL mask, not a group's access. That copy is
private and must load. A key any other user, group or the world could read
must still be refused.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from agent_core.adapters.cockpit.github import _access_acl, load_app_private_key

USER_OBJ, USER, GROUP_OBJ, GROUP, MASK, OTHER = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20
UNDEFINED = 0xFFFFFFFF
SERVICE = 997
READ = 4


def _acl(*entries: tuple[int, int, int]) -> bytes:
    """A `system.posix_acl_access` value: version 2, then (tag, perm, id) entries."""

    body = b"".join(struct.pack("<HHI", tag, perm, ident) for tag, perm, ident in entries)
    return struct.pack("<I", 2) + body


def _systemd_acl(*, named_user: int = SERVICE, group_perm: int = 0) -> bytes:
    return _acl(
        (USER_OBJ, READ, UNDEFINED),
        (USER, READ, named_user),
        (GROUP_OBJ, group_perm, UNDEFINED),
        (MASK, READ, UNDEFINED),
        (OTHER, 0, UNDEFINED),
    )


def _key(tmp_path: Path, mode: int) -> Path:
    key = tmp_path / "cockpit-github-app"
    key.write_bytes(b"test key material, not a key\n")
    key.chmod(mode)
    return key


def test_systemds_credential_copy_readable_only_by_the_service_loads(tmp_path: Path) -> None:
    key = _key(tmp_path, 0o440)
    loaded = load_app_private_key(key, acl_reader=lambda _: _systemd_acl(), reader_uid=SERVICE)
    assert loaded == key.read_bytes()


def test_a_key_only_its_owner_can_read_loads(tmp_path: Path) -> None:
    key = _key(tmp_path, 0o400)
    assert load_app_private_key(key, acl_reader=lambda _: None, reader_uid=SERVICE)


@pytest.mark.parametrize(
    ("mode", "acl"),
    [
        # Group bits with no ACL are the group's own access.
        (0o440, None),
        # The ACL names a user other than the service reading it.
        (0o440, _systemd_acl(named_user=SERVICE + 1)),
        # The owning group can read it.
        (0o440, _systemd_acl(group_perm=READ)),
        # A named group can read it.
        (
            0o440,
            _acl(
                (USER_OBJ, READ, UNDEFINED),
                (GROUP_OBJ, 0, UNDEFINED),
                (GROUP, READ, 50),
                (MASK, READ, UNDEFINED),
                (OTHER, 0, UNDEFINED),
            ),
        ),
        # Anyone can read it, whatever the ACL says of named users.
        (0o444, _systemd_acl()),
        # An ACL that cannot be parsed proves nothing.
        (0o440, b"\x02\x00\x00\x00\x01"),
        # Nor does a header with no entries, or another version's layout.
        (0o440, b"\x02\x00\x00\x00"),
        (0o440, b"\x01\x00\x00\x00" + _systemd_acl()[4:]),
        # An ACL missing its mandatory other or mask entry is incomplete.
        (
            0o440,
            _acl(
                (USER_OBJ, READ, UNDEFINED),
                (USER, READ, SERVICE),
                (GROUP_OBJ, 0, UNDEFINED),
                (MASK, READ, UNDEFINED),
            ),
        ),
        (
            0o440,
            _acl(
                (USER_OBJ, READ, UNDEFINED),
                (USER, READ, SERVICE),
                (GROUP_OBJ, 0, UNDEFINED),
                (OTHER, 0, UNDEFINED),
            ),
        ),
    ],
)
def test_a_key_anyone_else_can_read_is_refused(
    tmp_path: Path, mode: int, acl: bytes | None
) -> None:
    key = _key(tmp_path, mode)
    with pytest.raises(ValueError, match="readable only by its owner"):
        load_app_private_key(key, acl_reader=lambda _: acl, reader_uid=SERVICE)


def test_a_plain_file_has_no_access_acl(tmp_path: Path) -> None:
    assert _access_acl(_key(tmp_path, 0o400)) is None
