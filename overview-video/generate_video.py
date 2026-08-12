#!/usr/bin/env python3
"""Generate the CALIBER intro video.

Pipeline:
    1. edge-tts → one MP3 segment per scene (narration)
    2. ffprobe measures each segment, ffmpeg pads + concatenates → narration.mp3
    3. The script patches SCENE_TIMINGS in a copy of presentation.html so the
       deck's auto-advance matches the actual narration lengths
    4. Playwright (headless chromium) records the patched HTML at 1920×1080
    5. ffmpeg merges the recorded WebM + narration MP3 → caliber.mp4

Usage:
    python generate_video.py                    # full pipeline
    python generate_video.py --audio-only       # narration only
    python generate_video.py --record-only      # video only, no narration
    python generate_video.py --preview          # open the deck in a browser
    python generate_video.py --voice <name>     # different TTS voice

Prerequisites:
    pip install -r requirements.txt
    playwright install chromium
    ffmpeg must be on PATH
"""

from __future__ import annotations

import argparse
import asyncio
import math
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

# Scene definitions — each must match the SCENE_TIMINGS array in
# ../docs-site/presentation.html, in order. Durations are the floor; the
# pipeline recomputes them from measured TTS length + a 3 s tail buffer.

# SCENES are now loaded from the narration source file so the markdown
# in `narration_script.md` becomes the single source of truth. This avoids
# duplicated narration text and reduces divergence between the deck, the
# compiled narration, and the Python scene definitions.


def parse_narration_md(md_path: Path) -> list[dict]:
    """Parse `narration_script.md` for scenes.

    Expects scene headings like:
      ## Scene 1 — Title · 30 s

    and a blockquote immediately after containing the narration text.
    """
    import io

    text = md_path.read_text(encoding="utf-8")
    scenes: list[dict] = []

    pattern = re.compile(
        r"##\s*Scene\s*(\d+)\s*—\s*(.*?)\s*·\s*(\d+)\s*s[\r\n]+((?:>.*(?:\r\n|\n))+)",
        re.MULTILINE,
    )
    for m in pattern.finditer(text):
        sid = int(m.group(1))
        title = m.group(2).strip()
        duration = int(m.group(3))
        quote_block = m.group(4)
        # Strip leading '> ' from each quoted line and join with spaces.
        lines = [ln.lstrip('> ').rstrip() for ln in quote_block.splitlines()]
        # Remove empty lines that may appear between quote paragraphs
        lines = [ln for ln in lines if ln]
        narration = " ".join(lines)
        scenes.append({"id": sid, "title": title, "duration": duration, "narration": narration})

    # Sort by id to be robust to ordering in the markdown file
    scenes.sort(key=lambda s: s["id"])  # type: ignore
    return scenes

# TOTAL_DURATION and SCENES are initialized at runtime in `main()` by
# parsing `narration_script.md` so that the markdown file remains authoritative.
SCENES: list[dict] | None = None
TOTAL_DURATION: int | None = None
HERE = Path(__file__).resolve().parent
HTML_PATH = HERE.parent / "docs-site" / "presentation.html"
OUTPUT_DIR = HERE / "output"
FINAL_VIDEO_NAME = "caliber.mp4"

INTER_SCENE_BUFFER = 3  # seconds of silence after narration ends
INTER_SCENE_DELAY = 250  # milliseconds of silence before each scene

# Acronyms and product names edge-tts mispronounces. edge-tts 6.x accepts plain
# text only (no SSML / <phoneme> / <sub>), so spelling is the only lever: we
# respell each term phonetically right before synthesis. Keep the SCENES
# narration readable with real terms and tune how anything is spoken here.
#   - hyphenated lowercase (e.g. "em-el-flow") reads as one fluid word
#   - spaced capitals (e.g. "M C P") reads out clean, separate letters
PRONUNCIATIONS = {
    "MLflow": "em-el-flow",
    "RBAC": "ar-back",
    "DSPy": "dee-es-pie",
    "BM25": "B M twenty-five",
    "MCP": "M C P",
    "API": "A P I",
    "S3": "S 3",
}

# Longest terms first so e.g. "MLflow" wins before any shorter overlapping key.
_PRONUNCIATION_RE = re.compile(
    "|".join(re.escape(term) for term in sorted(PRONUNCIATIONS, key=len, reverse=True))
)


def apply_pronunciations(text: str) -> str:
    """Respell mispronounced terms for edge-tts (plain text, no SSML support)."""
    return _PRONUNCIATION_RE.sub(lambda m: PRONUNCIATIONS[m.group(0)], text)


async def generate_audio(voice: str, rate: str = "+0%") -> tuple[Path, list[int]]:
    """One TTS segment per scene, padded so the deck timing matches."""
    try:
        import edge_tts
    except ImportError:
        print("ERROR: edge-tts not installed. Run: pip install edge-tts")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    audio_dir = OUTPUT_DIR / "audio_segments"
    audio_dir.mkdir(exist_ok=True)

    concat_list: list[str] = []
    computed_durations: list[int] = []

    for scene in SCENES:
        seg_path = audio_dir / f"scene_{scene['id']:02d}.mp3"
        print(f"  Generating audio: Scene {scene['id']} — {scene['title']}...")

        communicate = edge_tts.Communicate(
            text=apply_pronunciations(scene["narration"]), voice=voice, rate=rate
        )
        await communicate.save(str(seg_path))

        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(seg_path),
            ],
            capture_output=True,
            text=True,
        )
        actual_dur = float(probe.stdout.strip())
        effective_dur = math.ceil(actual_dur) + INTER_SCENE_BUFFER
        computed_durations.append(effective_dur)
        print(f"    Narration: {actual_dur:.1f}s → scene: {effective_dur}s")

        padded_path = audio_dir / f"scene_{scene['id']:02d}_padded.mp3"
        if scene["id"] == 1:
            af = f"apad=pad_dur={effective_dur},atrim=0:{effective_dur}"
        else:
            af = (
                f"adelay={INTER_SCENE_DELAY}|{INTER_SCENE_DELAY},"
                f"apad=pad_dur={effective_dur},atrim=0:{effective_dur}"
            )
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(seg_path),
                "-af",
                af,
                "-ar",
                "44100",
                "-ac",
                "1",
                str(padded_path),
            ],
            capture_output=True,
        )
        concat_list.append(f"file '{padded_path}'")

    concat_file = audio_dir / "concat.txt"
    concat_file.write_text("\n".join(concat_list))

    combined = OUTPUT_DIR / "narration.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(combined),
        ],
        capture_output=True,
        check=True,
    )
    print(f"  Audio saved: {combined}")
    return combined, computed_durations


def prepare_html(durations: list[int]) -> Path:
    """Write a copy of presentation.html with SCENE_TIMINGS overridden."""
    html_text = HTML_PATH.read_text()
    patched = re.sub(
        r"var SCENE_TIMINGS\s*=\s*\[[\d\s,]+\]",
        f"var SCENE_TIMINGS = {durations}",
        html_text,
    )
    patched_path = HTML_PATH.parent / "presentation_timed.html"
    patched_path.write_text(patched)
    print(f"  Patched HTML timings: {durations}")
    return patched_path


async def record_video(
    html_path: Path | None = None, total_duration: int | None = None
) -> Path:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "ERROR: playwright not installed. Run: "
            "pip install playwright && playwright install chromium"
        )
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    video_dir = OUTPUT_DIR / "raw_video"
    video_dir.mkdir(exist_ok=True)

    effective_html = html_path or HTML_PATH
    wait_total = total_duration or TOTAL_DURATION
    print(f"  Recording presentation ({wait_total}s)...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            record_video_dir=str(video_dir),
            record_video_size={"width": 1920, "height": 1080},
        )
        page = await context.new_page()
        await page.goto(f"file://{effective_html}?autoplay=true")
        await page.wait_for_timeout((wait_total + 5) * 1000)

        video_path_pw = await page.video.path()
        await page.close()
        await context.close()
        await browser.close()

    output_video = OUTPUT_DIR / "presentation.webm"
    if video_path_pw and Path(video_path_pw).exists():
        shutil.move(str(video_path_pw), str(output_video))
    else:
        webms = list(video_dir.glob("*.webm"))
        if not webms:
            print("ERROR: No video file was recorded.")
            sys.exit(1)
        shutil.move(str(webms[0]), str(output_video))
    print(f"  Video saved: {output_video}")
    return output_video


def merge(video_path: Path, audio_path: Path) -> Path:
    output = OUTPUT_DIR / FINAL_VIDEO_NAME
    print("  Merging video + audio...")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )
    print(f"\n  Final video: {output}")
    return output


def preview():
    url = f"file://{HTML_PATH}?autoplay=false"
    print(f"  Opening: {url}")
    print("  Controls: Space play/pause · ←/→ prev/next · R restart")
    webbrowser.open(url)


def check_ffmpeg():
    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found on PATH.")
        print("  macOS:   brew install ffmpeg")
        print("  Ubuntu:  sudo apt install ffmpeg")
        sys.exit(1)


async def main():
    parser = argparse.ArgumentParser(description="Generate the CALIBER intro video")
    parser.add_argument(
        "--voice",
        default="en-US-AndrewMultilingualNeural",
        help="Edge TTS voice (default: en-US-AndrewMultilingualNeural)",
    )
    parser.add_argument("--rate", default="+5%", help="Speech rate (default: +5%%)")
    parser.add_argument("--audio-only", action="store_true")
    parser.add_argument("--record-only", action="store_true")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()

    if args.preview:
        preview()
        return

    check_ffmpeg()

    # Load scenes and compute total duration from the narration markdown
    global SCENES, TOTAL_DURATION
    SCENES = parse_narration_md(HERE / "narration_script.md")
    TOTAL_DURATION = sum(s["duration"] for s in SCENES)

    print("\n╔══════════════════════════════════════════════════╗")
    print("║   CALIBER — Video Generation Pipeline            ║")
    print("╚══════════════════════════════════════════════════╝\n")
    print(f"  Scenes:   {len(SCENES)}")
    print(f"  Duration: ~{TOTAL_DURATION // 60}m {TOTAL_DURATION % 60}s")
    print(f"  Voice:    {args.voice}")
    print(f"  Output:   {OUTPUT_DIR}\n")

    if args.audio_only:
        print("── Step 1/1: Generating narration audio ──")
        audio_path, durations = await generate_audio(args.voice, args.rate)
        total = sum(durations)
        print(f"\nDone! Audio: {audio_path}  (~{total // 60}m {total % 60}s)")
        return

    if args.record_only:
        print("── Step 1/1: Recording presentation ──")
        await record_video()
        print("\nDone! Video: overview-video/output/presentation.webm")
        return

    print("── Step 1/3: Generating narration audio ──")
    audio_path, durations = await generate_audio(args.voice, args.rate)

    print("\n── Step 2/3: Recording presentation ──")
    timed_html = prepare_html(durations)
    total = sum(durations)
    video_path = await record_video(timed_html, total)

    print("\n── Step 3/3: Compositing final video ──")
    final = merge(video_path, audio_path)

    print(f"\n{'═' * 50}")
    print(f"  Video ready: {final}")
    print(f"  Duration:    ~{total // 60}m {total % 60}s")
    print(f"{'═' * 50}\n")


if __name__ == "__main__":
    asyncio.run(main())
