"""Decode only pinned regular MOV/MP4 files in the selected directory."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO

from PIL import Image, ImageDraw

from final_cut.plan import Clip, Shot, timecode_frames, validate


def environment() -> dict[str, str]:
    """Media decoders and local models need executable paths, never provider credentials."""
    return {
        k: os.environ[k] for k in ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL") if k in os.environ
    }


INVENTORY_TIMEOUT = "Inventory timed out. Select a smaller folder and inspect again."


def inventory_time_left(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(INVENTORY_TIMEOUT)
    return remaining


def run(
    argv: list[str], fds: tuple[int, ...] = (), timeout: int = 180, *, deadline: float | None = None
) -> bytes:
    budget = timeout if deadline is None else min(timeout, inventory_time_left(deadline))
    try:
        return subprocess.run(
            argv,
            check=True,
            capture_output=True,
            timeout=budget,
            env=environment(),
            pass_fds=fds,
            stdin=subprocess.DEVNULL,
        ).stdout
    except subprocess.TimeoutExpired as exc:
        if deadline is not None:
            raise TimeoutError(INVENTORY_TIMEOUT) from exc
        raise ValueError(f"{Path(argv[0]).name} timed out on the Mac.") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"{Path(argv[0]).name} failed or timed out on the Mac.") from exc


def source_args(fd: int) -> list[str]:
    return [
        "-protocol_whitelist",
        "file,pipe",
        "-format_whitelist",
        "mov",
        "-f",
        "mov",
        "-enable_drefs",
        "0",
        "-i",
        f"/dev/fd/{fd}",
    ]


def fingerprint(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"


def source(directory: int, name: str, expected: str | None = None) -> BinaryIO:
    if name != Path(name).name or name.startswith("."):
        raise ValueError("Invalid source name.")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (expected and fingerprint(info) != expected):
        os.close(fd)
        raise ValueError("A source changed. Restore it or select the folder again.")
    return os.fdopen(fd, "rb")


def probe(
    args: list[str], fds: tuple[int, ...] = (), *, deadline: float | None = None
) -> dict[str, Any]:
    return dict(
        json.loads(
            run(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", *args],
                fds,
                deadline=deadline,
            )
        )
    )


def inspect_clip(grant: str, directory: int, name: str, *, deadline: float | None = None) -> Clip:
    with source(directory, name) as file:
        fd = file.fileno()
        streams = probe(source_args(fd), (fd,), deadline=deadline)["streams"]
        info = fingerprint(os.fstat(fd))
    video = [s for s in streams if s["codec_type"] == "video"]
    audio = [s for s in streams if s["codec_type"] == "audio"]
    if len(video) != 1 or len(audio) > 1:
        raise ValueError("Use one video track and at most one audio track.")
    v = video[0]
    fps = Fraction(v["avg_frame_rate"])
    if (
        not 1 <= fps <= 120
        or fps != Fraction(v["r_frame_rate"])
        or v.get("color_transfer") in {"smpte2084", "arib-std-b67"}
        or v.get("field_order", "progressive") not in ("unknown", "progressive")
        or v.get("sample_aspect_ratio", "1:1") not in ("1:1", "N/A")
        or any(d.get("rotation") for d in v.get("side_data_list", []))
        or any(abs(float(s.get("start_time", 0))) > 0.0001 for s in [v, *audio])
    ):
        raise ValueError("Use progressive constant-rate, square-pixel clips with aligned tracks.")
    frames = int(v["nb_frames"])
    if not 0 < frames <= 1_000_000:
        raise ValueError("Unsupported source length.")
    # Average and nominal rates can agree even on a variable-rate recording.
    # Inspect presentation timestamps in display order, including B-frame sources.
    with source(directory, name, info) as file:
        fd = file.fileno()
        raw = run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_packets",
                "-show_entries",
                "packet=pts:packet_side_data=",
                "-of",
                "csv=p=0",
                *source_args(fd),
            ],
            (fd,),
            deadline=deadline,
        )
    pts = sorted(int(line.split(b",")[0]) for line in raw.splitlines() if line.strip())
    tick = Fraction(v["time_base"])
    # Compare in integer units to bound CPU time on million-frame sources.
    unit = tick.numerator * fps.numerator
    step = tick.denominator * fps.denominator
    if len(pts) != frames or any(abs(t * unit - i * step) >= unit for i, t in enumerate(pts)):
        raise ValueError("Variable-rate or discontinuous video. Use a constant-rate original.")
    if deadline is not None:
        inventory_time_left(deadline)
    tc = v.get("tags", {}).get("timecode", "00:00:00:00")
    digest = hashlib.sha256(f"{grant}/{name}/{info}".encode()).hexdigest()[:32]
    return Clip(
        id=digest,
        name=name,
        fingerprint=info,
        fps=str(fps),
        frames=frames,
        origin=timecode_frames(tc, fps),
        width=v["width"],
        height=v["height"],
        channels=audio[0]["channels"] if audio else 0,
    )


def ffmpeg(args: list[str], fds: tuple[int, ...] = ()) -> bytes:
    return run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-n", *args], fds)


def sheet(fd: int, clip: Clip, first: int, end: int, output: Path) -> None:
    fps = Fraction(clip.fps)
    frames = sorted({first + (end - first - 1) * i // 7 for i in range(8)})
    canvas = Image.new("RGB", (4 * 240, 2 * 164), "#181818")
    draw = ImageDraw.Draw(canvas)
    for i, frame in enumerate(frames):
        os.lseek(fd, 0, os.SEEK_SET)
        data = ffmpeg(
            [
                "-ss",
                f"{float(frame / fps):.9f}",
                *source_args(fd),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=240:136:force_original_aspect_ratio=decrease",
                "-f",
                "image2pipe",
                "-c:v",
                "mjpeg",
                "pipe:1",
            ],
            (fd,),
        )
        with Image.open(io.BytesIO(data)) as image:
            x, y = (i % 4) * 240, (i // 4) * 164
            canvas.paste(image, (x, y))
            draw.text((x + 3, y + 139), f"Frame {frame}   {float(frame / fps):.3f}s", fill="white")
    canvas.save(output, "JPEG", quality=88)


def render(
    directory: int, shots: list[Shot], clips: dict[str, Clip], movie: Path, *, proxy: bool = False
) -> None:
    fps = validate(shots, clips)
    inputs: list[str] = []
    filters: list[str] = []
    fds: list[int] = []
    with contextlib.ExitStack() as stack:
        for i, s in enumerate(shots):
            c = clips[s.clip]
            fd = stack.enter_context(source(directory, c.name, c.fingerprint)).fileno()
            fds.append(fd)
            inputs.extend(source_args(fd))
            scale = ",scale=-2:360" if proxy else ""
            filters.append(
                f"[{i}:v:0]trim=start_frame={s.first}:end_frame={s.end},"
                f"setpts=PTS-STARTPTS{scale},setsar=1[v{i}]"
            )
            start, end = (round(n * 48000 / fps) for n in (s.first, s.end))
            n_samples = round((s.end - s.first) * 48000 / fps)
            if c.channels:
                a = (
                    f"[{i}:a:0]aresample=48000,aformat=channel_layouts=stereo,"
                    f"atrim=start_sample={start}:end_sample={end},asetpts=PTS-STARTPTS,"
                    f"apad,atrim=end_sample={n_samples},volume={s.gain_db:g}dB"
                )
            else:
                a = f"anullsrc=r=48000:cl=stereo,atrim=end_sample={n_samples}"
            for direction, n in (("in", s.fade_in), ("out", s.fade_out)):
                if n:
                    duration = round(n * 48000 / fps)
                    offset = 0 if direction == "in" else n_samples - duration
                    a += f",afade=t={direction}:ss={offset}:ns={duration}"
            filters.append(a + f"[a{i}]")
        for label, flags in (("v", "v=1:a=0"), ("a", "v=0:a=1")):
            joins = "".join(f"[{label}{i}]" for i in range(len(shots)))
            filters.append(joins + f"concat=n={len(shots)}:{flags}[{label}]")
        ffmpeg(
            [
                *inputs,
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-r",
                str(fps),
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23" if proxy else "18",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(movie),
            ],
            tuple(fds),
        )
    expected = sum(s.end - s.first for s in shots)
    streams = probe(["-i", str(movie)])["streams"]
    video, audio = streams
    if (
        video["codec_type"] != "video"
        or int(video["nb_frames"]) != expected
        or Fraction(video["avg_frame_rate"]) != fps
        or audio["codec_type"] != "audio"
        or abs(Fraction(audio["duration"]) - expected / fps) > 1 / fps
    ):
        raise ValueError("Movie verification failed on frame count, frame rate or audio duration.")
    ffmpeg(["-xerror", "-i", str(movie), "-map", "0:v", "-map", "0:a", "-f", "null", "-"])
