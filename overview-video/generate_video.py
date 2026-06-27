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

SCENES = [
    {
        "id": 1,
        "title": "Title",
        "duration": 30,
        "narration": (
            "CALIBER — Contextual Adaptive Lifecycle for Intelligent Build, "
            "Evaluation, and Refinement. An MLflow-native control plane for "
            "trusted agentic workflows: design, verify, calibrate, evaluate, "
            "publish, and observe every agent resource from one same-origin "
            "platform — then keep learning from real evidence and refining "
            "every asset across its lifecycle."
        ),
    },
    {
        "id": 2,
        "title": "The Problem",
        "duration": 28,
        "narration": (
            "Agentic workflows are easy to build and hard to trust. Prompts, "
            "tools, and multi-step agents ship faster than anyone can govern "
            "them. They drift, they're tuned by feel, and the lineage — what "
            "changed, how it scored, who approved it — lives in chat threads "
            "and notebooks instead of one system of record. There's no eval "
            "gate, no lineage, and no clean way back."
        ),
    },
    {
        "id": 3,
        "title": "The Market Gap",
        "duration": 30,
        "narration": (
            "This is not a niche worry. Two independent firms put the "
            "AI-agents market near fifty billion dollars by 2030, growing "
            "roughly forty-six percent a year, and Gartner expects agentic AI "
            "in a third of enterprise software by 2028, up from under one "
            "percent in 2024. But the trust gap is just as large. Deloitte "
            "finds about eighty percent of organizations lack mature "
            "governance. The Cloud Security Alliance finds only twenty-eight "
            "percent can trace an agent's actions back to a human. The "
            "opportunity is enormous. So is the gap underneath it."
        ),
    },
    {
        "id": 4,
        "title": "A Patchwork Stack",
        "duration": 30,
        "narration": (
            "So why do projects stall? The stack is fragmented. Teams run one "
            "tool to orchestrate, another to trace, another to evaluate, "
            "another to manage prompts. Most cover only one or two lifecycle "
            "stages, so lineage scatters across chat threads and notebooks. "
            "With seventy-six percent of enterprise AI use cases bought rather "
            "than built, that is even more vendors to integrate. And here is "
            "the deepest gap: these tools measure. They surface scores and "
            "stop. None of them close the loop."
        ),
    },
    {
        "id": 5,
        "title": "Introducing CALIBER",
        "duration": 28,
        "narration": (
            "CALIBER is one same-origin control plane for agentic workflows. "
            "As an MLflow application plugin, it mounts its interface and "
            "API on the same server as MLflow, so composing, measuring, "
            "and governing every agent resource happens against one identity, "
            "one store, and one trace backend. Compose, measure, govern."
        ),
    },
    {
        "id": 6,
        "title": "One Platform, Many Assets",
        "duration": 28,
        "narration": (
            "Build composes; the Library supplies. Prompts, tools, skills, "
            "MCP servers, knowledge bases, and workflows are each "
            "first-class, versioned assets. Every one has its own workspace — "
            "pytest for an asset — with a status header, stage tabs, and "
            "durable test runs, so you develop and verify it in isolation "
            "before a workflow ever uses it."
        ),
    },
    {
        "id": 7,
        "title": "Build · Workflows",
        "duration": 28,
        "narration": (
            "Workflows are composed in a visual Studio. Wire prompts, tools, "
            "skills, and knowledge bases into a graph, preview-run a draft "
            "without publishing, then enqueue real runs governed by runtime "
            "approvals and checkpointing. Every run carries an MLflow trace "
            "with per-tool-call spans."
        ),
    },
    {
        "id": 8,
        "title": "Data & Knowledge",
        "duration": 26,
        "narration": (
            "Behind every workflow is the data plane. Knowledge bases provide "
            "hybrid retrieval — BM25 and dense vectors fused with "
            "reciprocal rank fusion, optionally tri-hybrid with a knowledge "
            "graph — and report their own calibration metrics. The object "
            "store is the file interface over S3-compatible storage, and "
            "test sets are the versioned datasets every scored run draws from."
        ),
    },
    {
        "id": 9,
        "title": "Evaluate & Calibrate",
        "duration": 34,
        "narration": (
            "Measurement comes in two layers. Evaluation runs a test set "
            "through scorers and compares runs. Calibration goes further: it "
            "searches for a better version of an asset — using optimizers like "
            "Meta-Prompt, GEPA, or DSPy — and gates promotion on a measured "
            "score against the held-out set. Candidates that clear the gate "
            "land at candidate-ready and move live only through an explicit "
            "operator apply. Never auto-promoted. This is the heart of "
            "CALIBER: a closed learning loop that refines each asset against "
            "real evidence, so quality compounds over its lifecycle instead "
            "of drifting."
        ),
    },
    {
        "id": 10,
        "title": "Observe",
        "duration": 26,
        "narration": (
            "Every run is a trace you can open. Observability is built on "
            "MLflow tracing: each workflow run records per-tool-call spans, "
            "and the Evaluations surface turns those traces into scorecards, "
            "per-example results, and run-to-run comparisons. A readiness "
            "endpoint honestly reports which providers are real versus "
            "simulated — no fabricated scores, ever."
        ),
    },
    {
        "id": 11,
        "title": "Aria · The Agentic Copilot",
        "duration": 30,
        "narration": (
            "Aria is the embedded copilot. On OpenAI and Claude it runs a "
            "real tool-calling loop inside one turn: it reads live CALIBER "
            "state, executes capabilities, observes the result — including a "
            "workflow run's trace and scored evaluations — and iterates, "
            "bounded to eight tool steps. Chat, plan, and build modes pair "
            "with manual, auto-safe, and auto-all approvals to bound exactly "
            "what it's allowed to do."
        ),
    },
    {
        "id": 12,
        "title": "Aria · A Single Turn",
        "duration": 26,
        "narration": (
            "Here's a single turn. In build mode with auto-safe approvals, "
            "ask Aria to build a tool and test it. It checks for a name "
            "clash, drafts the tool, validates the schema, and runs it in the "
            "sandbox — then reports exactly what it observed and leaves the "
            "draft at the tested gate. Every action is recorded in the turn."
        ),
    },
    {
        "id": 13,
        "title": "Governance",
        "duration": 28,
        "narration": (
            "One gate governs every change, whether a person makes it or Aria "
            "does. Validate, test, approve, publish — there is no copilot "
            "bypass. RBAC controls who can advance each gate, the audit "
            "trail records actor, action, and entity, and artifacts live in "
            "object storage. Workflow runtime approvals govern live execution "
            "separately from offline artifact promotion."
        ),
    },
    {
        "id": 14,
        "title": "Why Different: Unified & Closed-Loop",
        "duration": 30,
        "narration": (
            "CALIBER collapses the patchwork into one same-origin control "
            "plane. It mounts its interface and API on the same server as "
            "MLflow, as an application plugin: one identity, one store, one "
            "trace backend. Prompts, tools, skills, knowledge bases, and "
            "workflows are each first-class, versioned assets with their own "
            "pytest-style workspace. Then the signature difference, a closed "
            "loop. It evaluates, searches for a better version with optimizers "
            "like Meta-Prompt, GEPA, and DSPy, and gates promotion on a "
            "measured score against a held-out set. It does not just measure. "
            "It closes the loop."
        ),
    },
    {
        "id": 15,
        "title": "Why Different: Open & Governed",
        "duration": 30,
        "narration": (
            "One gate governs every change, validate, test, approve, publish, "
            "whether a person makes it or the Aria copilot does. There is no "
            "copilot bypass. RBAC scopes who advances each gate, the audit "
            "trail records actor, action, and entity, and artifacts live in "
            "object storage. That matters: only twenty-eight percent of "
            "organizations can trace an agent's actions back to a human, and "
            "sixty-one percent of executives now require a human in the loop. "
            "With EU AI Act high-risk obligations landing August 2026, and "
            "CALIBER open source under Apache 2.0, your data and lineage stay "
            "home."
        ),
    },
    {
        "id": 16,
        "title": "Vision",
        "duration": 30,
        "narration": (
            "CALIBER is open source and MLflow-native. Native to MLflow. "
            "Agentic with Aria's permissioned tool loop. Measured by "
            "evaluation and calibration. Refined by a contextual, adaptive "
            "lifecycle that learns from evidence and improves every asset over "
            "time. Governed by explicit approval. Observable through tracing. "
            "Apache 2 licensed, with no vendor lock-in. A contextual, adaptive "
            "lifecycle for intelligent build, evaluation, and refinement — "
            "agentic workflows you can measure, refine, govern, and trust."
        ),
    },
]

TOTAL_DURATION = sum(s["duration"] for s in SCENES)
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
