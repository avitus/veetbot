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
