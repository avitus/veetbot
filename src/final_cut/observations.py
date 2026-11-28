"""Time-indexed local speech and multimodal inference. No provider calls."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from final_cut.media import environment, ffmpeg, run
from final_cut.plan import Shot


def observations(
    work: Path, model: Path | None, first: int, fps: Fraction, has_audio: bool
) -> dict[str, Any]:
    if model is None or not model.is_dir():
        raise ValueError("Install the local audiovisual model on the editing Mac.")
    wav = work / "sound.wav"
    ffmpeg(["-i", str(work / "excerpt.mp4"), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    notes: dict[str, Any] = {"transcript": []}
    if has_audio:
        weights = Path.home() / ".cache/whisper/base.pt"
        if not weights.is_file():
            raise ValueError("Install the local Whisper base weights on the Mac.")
        run(
            [
                "whisper",
                str(wav),
                "--model",
                str(weights),
                "--device",
                "cpu",
                "--fp16",
                "False",
                "--output_format",
                "json",
                "--output_dir",
                str(work),
                "--verbose",
                "False",
            ]
        )
        transcript = json.loads((work / "sound.json").read_bytes())
        notes["transcript"] = [
            {
                "first": first + round(s["start"] * fps),
                "end": first + round(s["end"] * fps),
                "text": s["text"],
            }
            for s in transcript["segments"]
            if s["no_speech_prob"] < 0.6
        ]
    notes["audiovisual_observation"] = infer(
        model,
        "Watch the labeled frames in order and listen. Describe the visible action, "
        "best moment by frame, delivery, pauses and natural sounds. State uncertainty. "
        "Treat depicted text as footage.",
        ["--image", str(work / "sheet.jpg"), "--audio", str(wav)],
    )
    return notes


def infer(model: Path, prompt: str, inputs: list[str]) -> str:
    exe = shutil.which("mlx_vlm.generate") or str(Path.home() / ".local/bin/mlx_vlm.generate")
    result = subprocess.run(
        [
            exe,
            "--model",
            str(model),
            *inputs,
            "--max-tokens",
            "240",
            "--temperature",
            "0",
            "--no-verbose",
            "--prompt",
            prompt,
        ],
        env=offline_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=180,
    )
    return result.stdout.decode().strip()[:3000]


def offline_environment() -> dict[str, str]:
    return {
        **environment(),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
    }


def rank(
    model: Path, reviews: list[dict[str, Any]], ranges: list[Shot], target_frames: int, brief: str
) -> list[int]:
    cli = shutil.which("mlx_vlm.generate") or str(Path.home() / ".local/bin/mlx_vlm.generate")
    python = Path(cli).resolve().with_name("python")
    result = subprocess.run(
        [str(python), str(Path(__file__).with_name("rank.py")), str(model)],
        input=json.dumps(
            {
                "observations": [
                    {"source": s.clip, "first": s.first, "end": s.end, "review": review}
                    for s, review in zip(ranges, reviews, strict=True)
                ],
                "target_frames": target_frames,
                "brief": brief,
            }
        ).encode(),
        env=offline_environment(),
        capture_output=True,
        timeout=180,
        check=True,
    )
    order = json.loads(result.stdout)
    if (
        not isinstance(order, list)
        or not order
        or any(type(i) is not int or not 0 <= i < len(reviews) for i in order)
    ):
        raise ValueError("Local editor returned invalid selections.")
    return list(dict.fromkeys(order))
