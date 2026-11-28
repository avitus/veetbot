"""Integer-frame edit decisions shared by FCPXML, preview, and delivery."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Handle = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Frame = Annotated[int, Field(ge=0, le=100_000_000)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Shot(Strict):
    clip: Handle
    first: Frame
    end: Frame
    gain_db: Annotated[float, Field(ge=-96, le=6)] = 0.0
    fade_in: Frame = 0
    fade_out: Frame = 0

    @model_validator(mode="after")
    def valid_range(self) -> Shot:
        if self.first >= self.end or self.fade_in + self.fade_out > self.end - self.first:
            raise ValueError("Invalid shot range or overlapping fades.")
        return self


class Change(Strict):
    index: Annotated[int, Field(ge=0, lt=64)]
    action: Literal["replace", "remove", "move", "insert"]
    shot: Shot | None = None
    destination: Annotated[int, Field(ge=0, lt=64)] | None = None

    @model_validator(mode="after")
    def exact_edit(self) -> Change:
        if (self.shot is not None) != (self.action in {"insert", "replace"}):
            raise ValueError("Only an insert or replacement supplies a shot.")
        if (self.destination is not None) != (self.action == "move"):
            raise ValueError("Only a move supplies a destination.")
        return self


def revised(parent: list[Shot], changes: list[Change]) -> list[Shot]:
    shots = parent.copy()
    for change in changes:
        i = change.index
        if change.action == "insert" and change.shot and i <= len(shots):
            shots.insert(i, change.shot)
        elif i >= len(shots):
            raise ValueError("Shot index exceeds the timeline.")
        elif change.action == "replace" and change.shot:
            shots[i] = change.shot
        elif change.action == "remove":
            shots.pop(i)
        elif change.action == "move" and change.destination is not None:
            if change.destination >= len(shots):
                raise ValueError("Destination exceeds the timeline.")
            shots.insert(change.destination, shots.pop(i))
        else:
            raise ValueError("Incomplete shot edit.")
    return shots


class Clip(Strict):
    id: Handle
    name: str
    fingerprint: str
    fps: str
    frames: int
    origin: int
    width: int
    height: int
    channels: int


def timecode_frames(value: str, fps: Fraction) -> int:
    match = re.fullmatch(r"(\d\d):(\d\d):(\d\d)([:;])(\d\d)", value)
    if not match:
        raise ValueError("Unsupported source timecode.")
    h, m, s, f = (int(match[n]) for n in (1, 2, 3, 5))
    nominal = round(fps)
    if h >= 24 or m >= 60 or s >= 60 or f >= nominal:
        raise ValueError("Invalid source timecode.")
    frames = ((h * 60 + m) * 60 + s) * nominal + f
    if match[4] == ";":
        if fps not in (Fraction(30000, 1001), Fraction(60000, 1001)):
            raise ValueError("Unsupported drop-frame timecode.")
        if m % 10 and s == 0 and f < nominal // 15:
            raise ValueError("The source timecode names a dropped frame label.")
        minutes = 60 * h + m
        frames -= (nominal // 15) * (minutes - minutes // 10)
    return frames


def seconds(frames: int, fps: Fraction) -> str:
    return f"{Fraction(frames) / fps}s"


def validate(shots: list[Shot], clips: dict[str, Clip]) -> Fraction:
    if not 1 <= len(shots) <= 64:
        raise ValueError("Use one to 64 shots.")
    if any(s.clip not in clips or s.end > clips[s.clip].frames for s in shots):
        raise ValueError("A clip is missing or a trim exceeds the source.")
    formats = {(clips[s.clip].fps, clips[s.clip].width, clips[s.clip].height) for s in shots}
    if len(formats) != 1:
        raise ValueError("This editor requires one frame rate and frame size per timeline.")
    fps = Fraction(formats.pop()[0])
    if sum(s.end - s.first for s in shots) > fps * 600:
        raise ValueError("The edit exceeds ten minutes.")
    return fps


def timeline(
    version: str, shots: list[Shot], clips: dict[str, Clip], folder: Path, library: Path
) -> bytes:
    fps = validate(shots, clips)
    first = clips[shots[0].clip]
    root = ET.Element("fcpxml", version="1.10")
    options = ET.SubElement(root, "import-options")
    ET.SubElement(options, "option", key="copy assets", value="0")
    ET.SubElement(options, "option", key="library location", value=library.as_uri())
    resources = ET.SubElement(root, "resources")
    # FCP validates the sequence against its named format catalog on import.
    raster = {(1280, 720): "720", (1920, 1080): "1080"}.get(
        (first.width, first.height), f"{first.width}x{first.height}"
    )
    rate = f"{float(fps):.2f}".rstrip("0").rstrip(".").replace(".", "")
    ET.SubElement(
        resources,
        "format",
        name=f"FFVideoFormat{raster}p{rate}",
        id="fmt",
        frameDuration=seconds(1, fps),
        width=str(first.width),
        height=str(first.height),
    )
    for cid in dict.fromkeys(s.clip for s in shots):
        c = clips[cid]
        asset = ET.SubElement(
            resources,
            "asset",
            id="r" + cid,
            name=c.name,
            format="fmt",
            start=seconds(c.origin, fps),
            duration=seconds(c.frames, fps),
            hasVideo="1",
            hasAudio=str(int(bool(c.channels))),
        )
        ET.SubElement(asset, "media-rep", kind="original-media", src=(folder / c.name).as_uri())
    project = ET.SubElement(root, "project", name=f"Veetbot cut {version}")
    total = sum(s.end - s.first for s in shots)
    seq = ET.SubElement(
        project,
        "sequence",
        format="fmt",
        duration=seconds(total, fps),
        tcStart="0s",
        tcFormat="NDF",
        audioLayout="stereo",
        audioRate="48k",
    )
    spine = ET.SubElement(seq, "spine")
    offset = 0
    for s in shots:
        c = clips[s.clip]
        length = s.end - s.first
        node = ET.SubElement(
            spine,
            "asset-clip",
            ref="r" + s.clip,
            name=c.name,
            offset=seconds(offset, fps),
            start=seconds(c.origin + s.first, fps),
            duration=seconds(length, fps),
        )
        if c.channels and (s.gain_db or s.fade_in or s.fade_out):
            volume = ET.SubElement(node, "adjust-volume", amount=f"{s.gain_db:g}dB")
            if s.fade_in or s.fade_out:
                param = ET.SubElement(volume, "param", name="amount", value=f"{s.gain_db:g}dB")
                for tag, n in (("fadeIn", s.fade_in), ("fadeOut", s.fade_out)):
                    if n:
                        ET.SubElement(param, tag, type="linear", duration=seconds(n, fps))
        offset += length
    return bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
