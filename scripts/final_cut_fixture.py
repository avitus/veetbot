"""Generate three numbered 29.97 fps originals, or verify every movie frame and audio cue."""

from __future__ import annotations

import argparse
import json
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

RATE = Fraction(30000, 1001)
SIZE = (640, 360)


def generate(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=False)
    for number, color in enumerate(("#235691", "#8d321e", "#385d34"), start=1):
        path = folder / f"Take {number}.mov"
        args = [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-n",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            "640x360",
            "-r",
            str(RATE),
            "-i",
            "pipe:0",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={number * 400}:sample_rate=48000",
            "-af",
            "volume=0.8:enable='lt(mod(t,1),0.2)',volume=0:enable='gte(mod(t,1),0.2)'",
            "-shortest",
            "-c:a",
            "pcm_s16le",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-timecode",
            f"0{number}:00:00;00",
            str(path),
        ]
        with subprocess.Popen(args, stdin=subprocess.PIPE) as p:
            assert p.stdin
            for frame in range(360):
                image = Image.new("RGB", SIZE, color)
                d = ImageDraw.Draw(image)
                d.text((30, 68), f"TAKE {number}   FRAME {frame:03}", font_size=40)
                d.ellipse((frame, 145, frame + 50, 195), fill="#efc75e")
                word = number * 512 + frame
                for bit in range(12):
                    x = 20 + bit * 36
                    d.rectangle((x, 260, x + 24, 306), fill="white" if word & 1 << bit else "black")
                p.stdin.write(image.tobytes())
            p.stdin.close()
            if p.wait():
                raise RuntimeError("Could not encode fixture.")


def verify(movie: Path) -> None:
    plan = json.loads((movie.parent / "plan.json").read_text())
    clips = json.loads((movie.parent.parent / "clips.json").read_text())
    expected = []
    for shot in plan["shots"]:
        number = int(Path(clips[shot["clip"]]["name"]).stem.split()[-1])
        expected.extend((number, f) for f in range(shot["first"], shot["end"]))
    # Sample the visible frame barcode after decoding the delivery encode.
    raw = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(movie),
            "-vf",
            "format=gray,crop=432:1:20:276",
            "-f",
            "rawvideo",
            "-",
        ]
    )
    rows = np.frombuffer(raw, dtype=np.uint8).reshape((-1, 432))
    values = (rows[:, 12::36] > 128) @ (1 << np.arange(12))
    assert list(values) == [n * 512 + f for n, f in expected], "Visible frame identity differs."
    audio = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(movie),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "f32le",
            "-",
        ]
    )
    samples = np.frombuffer(audio, dtype="<f4")
    checks = 0
    for i, (n, f) in enumerate(expected):
        phase = float((f + 0.5) / RATE) % 1
        # The source cue is a 200 ms tone once per second. Ignore codec/filter edges.
        if 0.06 < phase < 0.13 or 0.32 < phase < 0.88:
            center = round((i + 0.5) * 48000 / float(RATE))
            chunk = samples[center - 480 : center + 480]
            if phase < 0.13:
                hz = np.argmax(abs(np.fft.rfft(chunk))) * 50
                assert abs(hz - n * 400) <= 50 and np.max(abs(chunk)) > 0.01
            else:
                assert np.max(abs(chunk)) < 0.005
            checks += 1
    assert checks
    print(f"Matched {len(expected)} visible frame codes and {checks} audio cue windows.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("generate", "verify"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    (generate if args.operation == "generate" else verify)(args.path)
