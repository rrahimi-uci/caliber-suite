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
            "Evaluation, and Refinement. An MLflow-integrated control plane for "
            "trusted agentic workflows: design, verify, calibrate, evaluate, "
            "publish, and observe agent resources from one browser platform — "
            "then use real evidence to guide each asset's supported lifecycle."
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
        "duration": 48,
        "narration": (
            "This is not a niche worry. Two independent firms put the AI-agents "
            "market near fifty billion dollars by 2030, and Gartner expects "
            "agentic AI in a third of enterprise software by 2028. But the trust "
            "gap is just as large. On the same forecast, more than forty percent "
            "of agentic-AI projects will be canceled by 2027. Deloitte finds only "
            "one organization in five has mature governance for agentic AI, and "
            "the Cloud Security Alliance puts the share that can trace an agent's "
            "actions back to a human at twenty-eight percent. The opportunity is "
            "enormous. So is the gap underneath it."
        ),
    },
    {
        "id": 4,
        "title": "A Patchwork Stack",
        "duration": 45,
        "narration": (
            "So why do projects stall? The stack is fragmented. Teams run one "
            "tool to orchestrate, another to trace, another to evaluate, another "
            "to manage prompts. Most cover only one or two lifecycle stages, so "
            "lineage scatters across chat threads and notebooks. Menlo Ventures "
            "finds seventy-six percent of enterprise AI use cases are bought "
            "rather than built — more vendors to integrate. And here is the "
            "deepest gap: these tools measure, then stop. Optimizers exist. "
            "Almost none are wired to regression evidence, human authorization, "
            "and audited rollback — so almost none close the loop."
        ),
    },
    {
        "id": 5,
        "title": "Introducing CALIBER",
        "duration": 29,
        "narration": (
            "CALIBER is one ASGI control-plane codebase for agentic workflows. "
            "Mount it inside MLflow as a single process, or run it beside vanilla "
            "MLflow over HTTP. The API and the interface are identical — you "
            "choose how the two fail and are operated, not what you get. Either "
            "way, one interface unifies authoring, evidence, and governance. "
            "Compose, measure, govern."
        ),
    },
    {
        "id": 6,
        "title": "One Platform, Many Assets",
        "duration": 23,
        "narration": (
            "Build composes; the Library supplies. Prompts, tools, skills, "
            "MCP servers, knowledge bases, and workflows are first-class "
            "registered assets. Their workspaces expose the controls and "
            "evidence each family actually implements, so you can verify an "
            "asset without pretending every family has one uniform lifecycle."
        ),
    },
    {
        "id": 7,
        "title": "Build · Workflows",
        "duration": 30,
        "narration": (
            "Workflows are composed in a visual Studio. Wire prompts, tools, "
            "skills, and knowledge bases into a graph, preview-run a draft "
            "without publishing, then enqueue real runs governed by runtime "
            "approvals and checkpointing. Runs arrive from the Studio, an API "
            "call, a published service, or a cron trigger — all into one queue, "
            "and every run carries an MLflow trace."
        ),
    },
    {
        "id": 8,
        "title": "Data & Knowledge",
        "duration": 27,
        "narration": (
            "Behind every workflow is the data plane. Knowledge bases provide "
            "hybrid retrieval — BM25 and dense vectors fused with "
            "reciprocal rank fusion, optionally tri-hybrid with a knowledge "
            "graph — and report their own calibration metrics. The object "
            "store is CALIBER's own file interface over local or "
            "S3-compatible storage, and test sets are the versioned datasets "
            "every scored run draws from."
        ),
    },
    {
        "id": 9,
        "title": "Evaluate & Calibrate",
        "duration": 34,
        "narration": (
            "Measurement comes in two layers. Evaluation runs a test set "
            "through scorers and compares runs. The prompt refinement path can "
            "search for a better candidate using provider paths such as "
            "Meta-Prompt, GEPA, or DSPy, then apply per-dimension regression "
            "checks before candidate-ready. Moving that candidate live still "
            "requires an explicit operator action; registry gate verdicts "
            "outside the job are advisory. Tools use deterministic, "
            "revision-fenced fixture calibration instead. This is CALIBER's "
            "evidence loop: measured proposals with explicit human authority."
        ),
    },
    {
        "id": 10,
        "title": "Observe",
        "duration": 34,
        "narration": (
            "Every run is a trace you can open. Each run records per-tool-call "
            "spans, and the Evaluations surface turns those traces into "
            "scorecards, per-example results, and run comparisons — with "
            "readiness reporting which providers are real versus simulated, so no "
            "score is ever fabricated. Operating it is the other half: the queue "
            "reports depth, oldest wait, and worker heartbeats, and each "
            "evaluation turns a breached objective into a durable incident."
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
        "duration": 34,
        "narration": (
            "Here's a single turn. In build mode with auto-safe approvals, ask "
            "Aria to build a tool and test it. It checks for a name clash, drafts "
            "the tool, validates the schema, and runs it in a sandbox — a "
            "separate interpreter with hard time, memory, and output limits — "
            "then reports what it observed and leaves the draft at the tested "
            "gate. Every action is recorded in the turn."
        ),
    },
    {
        "id": 13,
        "title": "Governance",
        "duration": 38,
        "narration": (
            "Governance follows each asset's real lifecycle rather than one "
            "universal gate. Identity is server-validated — password accounts, "
            "revocable sessions, four scopes — and Aria reuses those same scopes, "
            "permission checks, and audit path as human-driven routes. Tool code "
            "runs behind a swappable execution boundary; the shipped one is a "
            "bounded subprocess. Validation, tests, an explicit apply or publish, "
            "and alias rollback exist where the asset implements them, and "
            "credentials stay encrypted behind a reference no route reads back."
        ),
    },
    {
        "id": 14,
        "title": "Why Different: Unified & Closed-Loop",
        "duration": 27,
        "narration": (
            "CALIBER collapses the patchwork into one control-plane interface, "
            "deployed either inside MLflow or beside it. CALIBER metadata and "
            "MLflow evidence keep explicit owners, while every asset family "
            "keeps the lifecycle controls it actually implements. Then the "
            "signature difference: an integrated prompt-refinement path that "
            "evaluates, searches with provider paths such as Meta-Prompt, "
            "GEPA, and DSPy, records per-dimension regression evidence, and "
            "requires an explicit human apply before anything goes live."
        ),
    },
    {
        "id": 15,
        "title": "Why Different: Open & Governed",
        "duration": 27,
        "narration": (
            "Whether a person or Aria initiates a change, the audit trail "
            "records the same actor, action, and entity. That matters: only "
            "twenty-eight percent of organizations can trace an agent's "
            "actions back to a human, and sixty-one percent of executives "
            "surveyed in 2025 required a human in the loop. EU AI Act "
            "high-risk obligations phase in from August 2026, and CALIBER is "
            "open source under Apache 2.0 — your data and lineage stay in "
            "infrastructure you control."
        ),
    },
    {
        "id": 16,
        "title": "Vision",
        "duration": 33,
        "narration": (
            "CALIBER is open source and MLflow-integrated, deployable embedded "
            "or standalone. Agentic with Aria's permissioned tool loop. "
            "Measured by evaluation and calibration. Refined through "
            "asset-specific lifecycles that preserve evidence and human "
            "authority. Governed by server-validated identity, a bounded "
            "execution boundary, and recorded actions. Observable through "
            "tracing and durable incidents. "
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
