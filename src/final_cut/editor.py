"""Private, revocable Mac session. All paths originate in a local owner selection."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from final_cut import media
from final_cut.plan import Change, Clip, Handle, Shot, revised, timeline, validate

DTD = Path(
    "/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/"
    "Versions/A/Resources/FCPXMLv1_10.dtd"
)


def write_json(path: Path, value: Any) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        output.write(json.dumps(value, sort_keys=True).encode())
        output.flush()
        os.fsync(output.fileno())
    os.replace(output.name, path)


def choose_folder() -> Path:
    result = media.run(
        [
            "/usr/bin/osascript",
            "-e",
            'POSIX path of (choose folder with prompt "Select raw clips for Veetbot to read")',
        ]
    )
    return Path(result.decode().strip())


def compare_export(
    path: Path, shots: list[Shot], clips: dict[str, Clip], folder: Path, version: str, library: Path
) -> None:
    root = ET.parse(path).getroot()
    projects = [
        p
        for lib in root.findall("library")
        if lib.get("location", "").rstrip("/") == library.as_uri()
        for p in lib.iter("project")
        if p.get("name") == f"Veetbot cut {version}" and p.get("uid")
    ]
    if not projects or not library.is_dir():
        raise ValueError("Choose the project's re-export from its versioned Final Cut library.")
    if len(projects) != 1:
        raise ValueError("Select the XML exported from this exact revised project.")
    seq = projects[0].find("sequence")
    if seq is None:
        raise ValueError("Export contains no project sequence.")
    assets = {a.attrib["id"]: a for a in root.findall("./resources/asset")}
    fps = validate(shots, clips)
    offset = 0
    nodes = seq.findall("./spine/*")
    if len(shots) != len(nodes):
        raise ValueError("Round trip changed the number of shots.")
    for s, node in zip(shots, nodes, strict=True):
        asset = assets.get(node.get("ref", ""))
        if node.tag != "asset-clip" or asset is None:
            raise ValueError("Unsupported or offline timeline item.")
        rep = asset.find("media-rep[@kind='original-media']")
        if rep is None:
            rep = asset.find("media-rep")
        url = urlsplit(rep.get("src", "") if rep is not None else "")
        c = clips[s.clip]
        original = folder / c.name
        linked = Path(unquote(url.path))
        if (
            url.scheme != "file"
            or url.netloc not in ("", "localhost")
            or linked != original
            or not stat.S_ISREG(original.lstat().st_mode)
            or media.fingerprint(original.lstat()) != c.fingerprint
        ):
            raise ValueError("Final Cut export does not resolve to the selected original.")
        for attr, expected in (
            ("offset", offset),
            ("start", c.origin + s.first),
            ("duration", s.end - s.first),
        ):
            if Fraction(node.get(attr, "0s").removesuffix("s")) * fps != expected:
                raise ValueError(f"Round trip changed a shot's {attr}.")
        offset += s.end - s.first
    if Fraction(seq.attrib["duration"].removesuffix("s")) * fps != offset:
        raise ValueError("Round trip changed the sequence duration.")


class Editor:
    def __init__(self, state: Path, port: int, model: Path | None = None) -> None:
        self.state = state
        self.port = port
        self.model = model
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if state.is_symlink() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
            raise ValueError("Use a private state directory owned by this Mac account.")

    def select(self, folder: Path, share_notes: bool = False) -> dict[str, Any]:
        folder = folder.resolve(strict=True)
        if self.state.resolve().is_relative_to(folder) or folder.is_relative_to(
            self.state.resolve()
        ):
            raise ValueError("Keep the selected source folder separate from editor outputs.")
        grant = uuid.uuid4().hex
        info = folder.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Select a directory.")
        project = self.state / grant
        project.mkdir(mode=0o700)
        write_json(
            project / "grant.json",
            {
                "folder": str(folder),
                "dev": info.st_dev,
                "ino": info.st_ino,
                "share_notes": share_notes,
            },
        )
        write_json(self.state / "active.json", grant)
        return {"grant": grant, "share_notes": share_notes}

    def active(self, grant: str | None = None) -> str:
        try:
            active = str(json.loads((self.state / "active.json").read_bytes()))
        except FileNotFoundError:
            raise ValueError(
                "Select a footage folder on the Mac. The previous grant may have been revoked."
            ) from None
        if grant is not None and grant != active:
            raise ValueError("Folder permission changed. Inspect the newly selected folder.")
        return active

    @contextlib.contextmanager
    def directory(self, grant: str) -> Iterator[tuple[int, Path]]:
        self.active(grant)
        cfg = json.loads((self.state / grant / "grant.json").read_bytes())
        folder = Path(cfg["folder"])
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (cfg["dev"], cfg["ino"]):
                raise ValueError("The selected folder moved or was replaced. Select it again.")
            yield fd, folder
        finally:
            os.close(fd)

    def inventory(self) -> dict[str, Any]:
        grant = self.active()
        clips: dict[str, Clip] = {}
        skipped = 0
        with self.directory(grant) as (fd, _):
            for name in sorted(os.listdir(fd)):
                if name.startswith(".") or Path(name).suffix.lower() not in {
                    ".mov",
                    ".mp4",
                    ".m4v",
                }:
                    continue
                if not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                    continue
                if len(clips) >= 128:
                    raise ValueError("Select a folder with at most 128 clips.")
                try:
                    c = media.inspect_clip(grant, fd, name)
                except (ValueError, KeyError, OSError, ZeroDivisionError):
                    skipped += 1
                    continue
                clips[c.id] = c
        write_json(self.state / grant / "clips.json", {k: v.model_dump() for k, v in clips.items()})
        return {
            "grant": grant,
            "unsupported": skipped,
            "clips": [
                {
                    "id": c.id,
                    "fps": c.fps,
                    "frames": c.frames,
                    "source_start": c.origin,
                    "seconds": float(c.frames / Fraction(c.fps)),
                    "audio_channels": c.channels,
                }
                for c in clips.values()
            ],
        }

    def clips(self, grant: str) -> dict[str, Clip]:
        self.active(grant)
        path = self.state / grant / "clips.json"
        if not path.exists():
            raise ValueError("Inspect the selected folder first.")
        return {k: Clip.model_validate(v) for k, v in json.loads(path.read_bytes()).items()}

    def url(self, grant: str, artifact: Path) -> str:
        return f"http://127.0.0.1:{self.port}/{grant}/{artifact.parent.name}/{artifact.name}"

    def excerpt(self, grant: str, shot: Shot, analyze: bool) -> dict[str, Any]:
        clips = self.clips(grant)
        fps = validate([shot], clips)
        if shot.end - shot.first > fps * 15:
            raise ValueError("Select at most 15 seconds per excerpt.")
        c = clips[shot.clip]
        project = self.state / grant
        cache_key = json.dumps(
            ["excerpt-1", shot.model_dump(), analyze, str(self.model)], sort_keys=True
        )
        dest = project / hashlib.sha256(cache_key.encode()).hexdigest()[:32]
        with self.directory(grant) as (fd, _), media.source(fd, c.name, c.fingerprint) as source:
            if not dest.exists():
                with tempfile.TemporaryDirectory(dir=project) as tmp:
                    work = Path(tmp)
                    media.render(fd, [shot], clips, work / "excerpt.mp4", proxy=True)
                    media.sheet(source.fileno(), c, shot.first, shot.end, work / "sheet.jpg")
                    if analyze:
                        from final_cut.observations import observations

                        notes = observations(work, self.model, shot.first, fps, bool(c.channels))
                        write_json(work / "notes.json", notes)
                        write_json(work / "range.json", shot.model_dump())
                    self.active(grant)
                    work.rename(dest)
        self.active(grant)
        result: dict[str, Any] = {
            "excerpt": self.url(grant, dest / "excerpt.mp4"),
            "contact_sheet": self.url(grant, dest / "sheet.jpg"),
        }
        if analyze:
            result["evidence_id"] = dest.name
            result["local_notes"] = self.url(grant, dest / "notes.json")
            if json.loads((project / "grant.json").read_bytes())["share_notes"]:
                result["observations"] = json.loads((dest / "notes.json").read_bytes())
            else:
                result["observations_status"] = "Local only. Use recommend to rank these ranges."
        return result

    def recommend(
        self, grant: str, evidence: list[str], seconds: int, brief: str = ""
    ) -> dict[str, Any]:
        """Rank reviewed ranges locally; only validated frame selections leave the Mac."""
        from final_cut.observations import rank

        clips = self.clips(grant)
        if self.model is None or not self.model.is_dir():
            raise ValueError("Install the local audiovisual model.")
        ranges: list[Shot] = []
        reviews = []
        for key in dict.fromkeys(evidence):
            directory = self.state / grant / key
            if not (directory / "range.json").is_file():
                raise ValueError("Use evidence IDs returned by analyzed excerpts.")
            ranges.append(Shot.model_validate_json((directory / "range.json").read_bytes()))
            reviews.append(json.loads((directory / "notes.json").read_bytes()))
        fps = validate(ranges, clips)
        remaining = round(seconds * fps)
        order = rank(self.model, reviews, ranges, remaining, brief)
        selected: list[dict[str, Any]] = []
        for index in order:
            s = ranges[index]
            if any(
                prior["clip"] == s.clip and prior["first"] < s.end and s.first < prior["end"]
                for prior in selected
            ):
                continue
            n = min(remaining, s.end - s.first)
            selected.append(s.model_copy(update={"end": s.first + n}).model_dump())
            remaining -= n
            if not remaining:
                break
        self.active(grant)
        return {"shots": selected, "additional_frames_needed": remaining}

    def cut(self, grant: str, shots: list[Shot], parent: str | None = None) -> dict[str, Any]:
        clips = self.clips(grant)
        fps = validate(shots, clips)
        project = self.state / grant
        plan = {
            "schema": 2,
            "grant": grant,
            "parent": parent,
            "fps": str(fps),
            "shots": [s.model_dump() for s in shots],
        }
        version = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:32]
        dest = project / version
        with self.directory(grant) as (fd, folder):
            for cid in {s.clip for s in shots}:
                with media.source(fd, clips[cid].name, clips[cid].fingerprint):
                    pass
            if not dest.exists():
                with tempfile.TemporaryDirectory(dir=project) as tmp:
                    work = Path(tmp)
                    write_json(work / "plan.json", plan)
                    (work / "timeline.fcpxml").write_bytes(
                        timeline(version, shots, clips, folder, dest / "Reel.fcpbundle")
                    )
                    media.run(
                        [
                            "/usr/bin/xmllint",
                            "--nonet",
                            "--noout",
                            "--dtdvalid",
                            DTD.as_uri(),
                            str(work / "timeline.fcpxml"),
                        ]
                    )
                    media.render(fd, shots, clips, work / "preview.mp4")
                    with (work / "preview.mp4").open("rb") as stream:
                        write_json(
                            work / "verified.json",
                            hashlib.file_digest(stream, "sha256").hexdigest(),
                        )
                    self.active(grant)
                    work.rename(dest)
        return {
            "version": version,
            "parent": parent,
            "preview": self.url(grant, dest / "preview.mp4"),
            "timeline": self.url(grant, dest / "timeline.fcpxml"),
            "frames": sum(s.end - s.first for s in shots),
            "shots": plan["shots"],
            "timeline_written": True,
        }

    def plan(self, grant: str, version: Handle) -> list[Shot]:
        self.active(grant)
        path = self.state / grant / version / "plan.json"
        if not path.is_file():
            raise ValueError("Unknown cut version for this selection.")
        return [Shot.model_validate(s) for s in json.loads(path.read_bytes())["shots"]]

    def revise(self, grant: str, version: str, changes: list[Change]) -> dict[str, Any]:
        return self.cut(grant, revised(self.plan(grant, version), changes), version)

    def deliver(self, grant: str, version: str) -> dict[str, Any]:
        shots = self.plan(grant, version)
        dest = self.state / grant / version
        with self.directory(grant) as (fd, _):
            clips = self.clips(grant)
            validate(shots, clips)
            for s in shots:
                with media.source(fd, clips[s.clip].name, clips[s.clip].fingerprint):
                    pass
        preview = dest / "preview.mp4"
        with preview.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != json.loads(
                (dest / "verified.json").read_bytes()
            ):
                raise ValueError(
                    "The approved preview changed. Render a new version before delivery."
                )
        self.active(grant)
        movie = dest / "movie.mp4"
        # The approved preview is already the full resolution, decoded-and-verified render.
        if not movie.exists():
            os.link(preview, movie)
        if not movie.samefile(preview):
            raise ValueError("The delivered file changed on disk. Check the existing movie.")
        receipt = dest / "import-requested"
        if not receipt.exists():
            if (dest / "Reel.fcpbundle").exists():
                raise ValueError("A library already exists for this version. Reconcile its export.")
            with receipt.open("x") as claim:
                claim.write(version)
                claim.flush()
                os.fsync(claim.fileno())
            # LaunchServices sends Open Document; delivery of that event is not import proof.
            with contextlib.suppress(ValueError):
                media.run(
                    ["/usr/bin/open", "-b", "com.apple.FinalCut", str(dest / "timeline.fcpxml")]
                )
        return {
            "movie": self.url(grant, movie),
            "timeline": self.url(grant, dest / "timeline.fcpxml"),
            "movie_verified": True,
            "version": version,
            "timeline_written": True,
            "import": "confirmed"
            if (dest / "confirmed").exists()
            else "Unconfirmed. Export this project as XML from Final Cut, then reconcile.",
        }

    def reconcile(self, grant: str, version: str) -> dict[str, Any]:
        self.active(grant)
        export = Path(
            media.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    'POSIX path of (choose file with prompt "Choose the exported project XML")',
                ]
            )
            .decode()
            .strip()
        )
        return self.confirm_export(grant, version, export)

    def confirm_export(self, grant: str, version: str, export: Path) -> dict[str, Any]:
        shots = self.plan(grant, version)
        if export.suffix == ".fcpxmld":
            export = export / "Info.fcpxml"
        if export.suffix != ".fcpxml" or export.stat().st_size > 8_000_000:
            raise ValueError("Choose a Final Cut XML export of this project.")
        with self.directory(grant) as (_, folder):
            try:
                compare_export(
                    export,
                    shots,
                    self.clips(grant),
                    folder,
                    version,
                    self.state / grant / version / "Reel.fcpbundle",
                )
            except (ET.ParseError, KeyError, ZeroDivisionError) as exc:
                raise ValueError("Choose valid project XML re-exported by Final Cut.") from exc
        self.active(grant)
        (self.state / grant / version / "confirmed").touch()
        return {
            "version": version,
            "import": "confirmed",
            "sources_online": True,
            "cut_points_match": True,
        }
