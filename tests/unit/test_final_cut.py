"""The assistant roster, Mac permission, immutable plan and import receipt boundaries."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_core.config import ConfigurationError, load_settings
from agent_core.domain.mcp import MCPRemoteTool
from agent_core.mcp.configuration import final_cut_config
from agent_core.mcp.mapping import map_discovered_tools
from final_cut.editor import Editor, compare_export, write_json
from final_cut.media import fingerprint, source
from final_cut.plan import Change, Clip, Shot, revised, timecode_frames, timeline
from final_cut.protocol import REQUEST
from final_cut.server import create_server
from tests.unit.test_config import base_environment


def test_operator_socket_enables_the_mac_editor() -> None:
    assert load_settings(base_environment()).final_cut_socket is None
    path = "/private/run/veetbot/cut.sock"
    assert (
        load_settings({**base_environment(), "AGENT_FINAL_CUT_SOCKET": path}).final_cut_socket
        == path
    )
    with pytest.raises(ConfigurationError):
        load_settings({**base_environment(), "AGENT_FINAL_CUT_SOCKET": "relative"})


@pytest.mark.asyncio
async def test_normal_mcp_registry_accepts_every_tool_and_discovers_the_skill(
    tmp_path: Path,
) -> None:
    server = create_server(tmp_path / "missing.sock")
    tools = tuple(
        MCPRemoteTool(name=t.name, description=t.description or "", input_schema=t.input_schema)
        for t in await server.list_tools()
    )
    config = final_cut_config("test", "/private/editor.sock")
    mapped = map_discovered_tools(config, tools)
    assert not mapped.rejected
    assert {tool.remote_name for tool in mapped.accepted} == {
        "inventory",
        "rough_cut",
        "select_folder",
        "excerpt",
        "deliver",
        "revise",
        "recommend",
        "reconcile",
    }
    assert {p.name for p in await server.list_prompts()} == {"editing"}
    assert config.required_scopes == {"mcp.final_cut.use"}
    assert config.operator_configured


def test_mac_grant_retains_no_access_after_rotation_or_revocation(tmp_path: Path) -> None:
    editor = Editor(tmp_path / "state", 1234)
    folder = tmp_path / "raw"
    folder.mkdir()
    grant = editor.select(folder)["grant"]
    external = tmp_path / "outside.mov"
    external.write_bytes(b"outside")
    (folder / "link.mov").symlink_to(external)
    assert editor.inventory()["clips"] == []
    with editor.directory(grant) as (fd, _), pytest.raises(OSError):
        source(fd, "link.mov")
    with pytest.raises(ValidationError):
        REQUEST.validate_python({"operation": "deliver", "grant": grant, "version": "../outside"})
    editor.select(folder)
    with pytest.raises(ValueError, match="permission changed"):
        editor.plan(grant, "0" * 32)
    (editor.state / "active.json").unlink()
    with pytest.raises(ValueError, match="Select a footage folder"):
        editor.inventory()


def test_round_trip_compares_the_source_link_and_every_rational_frame(tmp_path: Path) -> None:
    original = tmp_path / "take.mov"
    original.touch()
    cid = "1" * 32
    fps = Fraction(30000, 1001)
    clip = Clip(
        id=cid,
        name=original.name,
        fingerprint=fingerprint(original.stat()),
        fps=str(fps),
        frames=360,
        origin=timecode_frames("01:00:00;00", fps),
        width=640,
        height=360,
        channels=1,
    )
    assert clip.origin == 107892
    parent = [Shot(clip=cid, first=10, end=60), Shot(clip=cid, first=92, end=200, fade_in=2)]
    shots = revised(
        parent, [Change(index=0, action="replace", shot=Shot(clip=cid, first=10, end=120))]
    )
    assert parent[0].end == 60 and shots[1] == parent[1]
    version = "2" * 32
    export = tmp_path / "timeline.fcpxml"
    clips = {cid: clip}
    library = tmp_path / "Reel.fcpbundle"
    library.mkdir()
    export.write_bytes(timeline(version, shots, clips, tmp_path, library))
    with pytest.raises(ValueError, match="re-export"):
        compare_export(export, shots, clips, tmp_path, version, library)
    tree = ET.parse(export)
    project = tree.getroot().find("project")
    assert project is not None
    tree.getroot().remove(project)
    project.set("uid", "A7DB6459-00AB-40BB-B04E-56C81946701C")
    exported_library = ET.SubElement(tree.getroot(), "library", location=library.as_uri())
    ET.SubElement(exported_library, "event").append(project)
    tree.write(export)
    compare_export(export, shots, clips, tmp_path, version, library)
    fmt = tree.find("./resources/format")
    assert fmt is not None
    fmt.set("frameDuration", "1001/60000s")
    tree.write(export)
    with pytest.raises(ValueError, match="frame rate"):
        compare_export(export, shots, clips, tmp_path, version, library)
    fmt.set("frameDuration", "2002/60000s")
    node = tree.find(".//spine/asset-clip")
    assert node is not None
    node.set("start", f"{(clip.origin + 11) / fps}s")
    tree.write(export)
    with pytest.raises(ValueError, match="start"):
        compare_export(export, shots, clips, tmp_path, version, library)


def test_delivery_replay_after_lost_ack_does_not_reopen_final_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    from final_cut import media

    editor = Editor(tmp_path / "state", 59381)
    folder = tmp_path / "raw"
    folder.mkdir()
    original = folder / "original.mov"
    original.write_bytes(b"fixed original")
    grant = editor.select(folder)["grant"]
    cid = "a" * 32
    version = "b" * 32
    c = Clip(
        id=cid,
        name=original.name,
        fingerprint=fingerprint(original.stat()),
        fps="25",
        frames=150,
        origin=0,
        width=640,
        height=360,
        channels=2,
    )
    write_json(editor.state / grant / "clips.json", {cid: c.model_dump()})
    work = editor.state / grant / version
    work.mkdir()
    write_json(work / "plan.json", {"shots": [Shot(clip=cid, first=3, end=93).model_dump()]})
    (work / "preview.mp4").write_bytes(b"fixture verified render")
    write_json(
        work / "verified.json", hashlib.sha256((work / "preview.mp4").read_bytes()).hexdigest()
    )
    calls = []

    def lost(args: list[str]) -> bytes:
        calls.append(args)
        raise ValueError("Lost acknowledgement")

    monkeypatch.setattr(media, "run", lost)
    delivered = editor.deliver(grant, version)
    assert delivered["timeline"].endswith(f"/{version}/timeline.fcpxml")
    assert "unconfirmed" in delivered["import"].lower()
    assert "unconfirmed" in editor.deliver(grant, version)["import"].lower()
    assert "manually" in delivered["import"].lower()
    assert editor.deliver(grant, version)["import"] == delivered["import"]
    assert len(calls) == 1
    assert (work / "movie.mp4").samefile(work / "preview.mp4")
    original.unlink()
    with pytest.raises(FileNotFoundError):
        editor.deliver(grant, version)


def test_local_recommendations_return_only_reviewed_nonoverlapping_ranges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from final_cut import observations

    editor = Editor(tmp_path / "state", 59381, tmp_path)
    folder = tmp_path / "raw"
    folder.mkdir()
    grant = editor.select(folder)["grant"]
    ids = ["1" * 32, "2" * 32]
    clips = {
        cid: Clip(
            id=cid,
            name=f"{cid}.mov",
            fingerprint=cid,
            fps="25",
            frames=150,
            origin=0,
            width=640,
            height=360,
            channels=2,
        ).model_dump()
        for cid in ids
    }
    write_json(editor.state / grant / "clips.json", clips)
    evidence = []
    for i, shot in enumerate(
        [
            Shot(clip=ids[0], first=0, end=50),
            Shot(clip=ids[0], first=25, end=75),
            Shot(clip=ids[1], first=25, end=100),
        ]
    ):
        key = f"{i:032x}"
        evidence.append(key)
        work = editor.state / grant / key
        work.mkdir()
        write_json(work / "range.json", shot.model_dump())
        write_json(work / "notes.json", {"transcript": "Private speech"})
    monkeypatch.setattr(observations, "rank", lambda *_args: [0, 1, 2])
    result = editor.recommend(grant, evidence, 4, "Open with the strongest action.")
    assert [(s["clip"], s["first"], s["end"]) for s in result["shots"]] == [
        (ids[0], 0, 50),
        (ids[1], 25, 75),
    ]
    assert result["additional_frames_needed"] == 0
    assert set(result) == {"shots", "additional_frames_needed"}
    shots = [
        Shot(clip=ids[0], first=30, end=55),
        Shot(clip=ids[0], first=0, end=50, fade_in=40, fade_out=10),
    ]
    for key, shot in zip(evidence[:2], shots, strict=True):
        write_json(editor.state / grant / key / "range.json", shot.model_dump())
    monkeypatch.setattr(observations, "rank", lambda *_args: [0, 1])
    prefix = editor.recommend(grant, evidence[:2], 2)
    checked = [Shot.model_validate(row) for row in prefix["shots"]]
    assert [(s.first, s.end) for s in checked] == [(30, 55), (0, 25)]
    assert prefix["additional_frames_needed"] == 0


def test_inventory_skips_disappearing_clip_and_bounds_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from final_cut import media

    editor = Editor(tmp_path / "state", 59381)
    folder = tmp_path / "raw"
    folder.mkdir()
    editor.select(folder)
    monkeypatch.setattr(os, "listdir", lambda fd: ["gone.mov"])
    assert editor.inventory()["unsupported"] == 1
    (folder / "gone.mov").touch()

    def expire(*args: object, **kwargs: object) -> Clip:
        assert kwargs.get("deadline") is not None
        raise TimeoutError

    monkeypatch.setattr(media, "inspect_clip", expire)
    with pytest.raises(ValueError, match="smaller folder"):
        editor.inventory()


@pytest.mark.asyncio
async def test_mac_socket_failure_carries_effect_status(tmp_path: Path) -> None:
    import socket
    import threading

    from mcp.types import CallToolResult

    endpoint = tmp_path / "editor.sock"
    server = create_server(endpoint)
    args = {"grant": "a" * 32, "version": "b" * 32}
    result = await server.call_tool("deliver", args)
    assert isinstance(result, CallToolResult) and result.is_error
    assert result.structured_content == {"effect_status": "not_applied"}
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(endpoint))
        listener.listen()

        def drop() -> None:
            connection, _ = listener.accept()
            with connection:
                connection.recv(65536)

        thread = threading.Thread(target=drop)
        thread.start()
        result = await server.call_tool("deliver", args)
        thread.join(5)
    assert isinstance(result, CallToolResult) and result.is_error
    assert result.structured_content == {"effect_status": "unknown"}


def test_socket_restart_preserves_active_listeners_and_regular_files(tmp_path: Path) -> None:
    import socket

    from final_cut import worker

    path = tmp_path / "editor.sock"
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        listener.listen()
        with pytest.raises(ValueError, match="running"):
            worker.clear_stale_socket(path)
    worker.clear_stale_socket(path)
    assert not path.exists()
    path.touch()
    with pytest.raises(ValueError, match="socket"):
        worker.clear_stale_socket(path)
    assert path.is_file()


def test_damaged_preview_renders_a_new_immutable_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from final_cut import media

    editor = Editor(tmp_path / "state", 59381)
    folder = tmp_path / "raw"
    folder.mkdir()
    original = folder / "clip.mov"
    original.touch()
    grant = editor.select(folder)["grant"]
    cid = "a" * 32
    c = Clip(
        id=cid,
        name=original.name,
        fingerprint=fingerprint(original.stat()),
        fps="25",
        frames=75,
        origin=0,
        width=640,
        height=360,
        channels=1,
    )
    write_json(editor.state / grant / "clips.json", {cid: c.model_dump()})
    shots = [Shot(clip=cid, first=0, end=25)]
    monkeypatch.setattr(media, "run", lambda *_args: b"")
    monkeypatch.setattr(
        media, "render", lambda fd, shots, clips, output: output.write_bytes(b"video")
    )
    first = editor.cut(grant, shots)["version"]
    path = editor.state / grant / first / "preview.mp4"
    path.write_bytes(b"damaged")
    fresh = editor.cut(grant, shots)["version"]
    assert fresh != first and editor.plan(grant, fresh) == shots
    assert editor.cut(grant, shots)["version"] == fresh
    assert path.read_bytes() == b"damaged"


def test_export_input_size_policy(tmp_path: Path) -> None:
    export = tmp_path / "oversize.fcpxml"
    with export.open("wb") as f:
        f.truncate(8_000_001)
    with pytest.raises(ValueError, match="8 MB"):
        compare_export(export, [], {}, tmp_path, "", tmp_path)


def test_playback_withdraws_active_stream_and_never_serves_notes(tmp_path: Path) -> None:
    import http.client
    import socket
    import threading
    from http.server import HTTPServer

    from final_cut.worker import playback_handler

    editor = Editor(tmp_path / "state", 59381)
    raw = tmp_path / "raw"
    raw.mkdir()
    grant = editor.select(raw)["grant"]
    key = "f" * 32
    artifacts = editor.state / grant / key
    artifacts.mkdir()
    (artifacts / "notes.json").write_text("Local notes")
    size = 16 * 1024 * 1024
    with (artifacts / "movie.mp4").open("wb") as movie:
        movie.truncate(size)
    handler = playback_handler(editor)
    address = ("127.0.0.1", editor.port)
    with HTTPServer(address, handler, bind_and_activate=False) as server:

        def handle(connection: socket.socket) -> None:
            with connection:
                handler(connection, address, server)

        for name in ("notes.json", "movie.mp4"):
            reader, writer = socket.socketpair()
            with reader, writer:
                reader.settimeout(10)

                thread = threading.Thread(target=handle, args=(writer,))
                thread.start()
                client = http.client.HTTPConnection(*address)
                client.sock = reader
                try:
                    client.request("GET", f"/{grant}/{key}/{name}")
                    with client.getresponse() as response:
                        if name == "notes.json":
                            assert response.status == 404
                        else:
                            assert response.status == 200
                            editor.revoke()
                            assert len(response.read(size)) < size
                finally:
                    client.close()
                    thread.join(5)
