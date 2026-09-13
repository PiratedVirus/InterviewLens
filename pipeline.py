"""
Processing pipeline: video/audio -> transcript (Whisper, local) + code OCR
(Tesseract, local) -> a review_prompt.md -> fed straight into the Claude
Code CLI, which writes its judged review as review_data.json, rendered into
a rich review.html report (via review_template.html) in the same session
folder.

Nothing here calls any paid API. Whisper and Tesseract both run on-device.
Claude Code CLI runs on your existing Claude subscription (Pro/Max/Team),
same as Claude Desktop — no separate API billing.
"""

import json
import re
import subprocess
from pathlib import Path

BASE_DIR = Path.home() / "DSAInterviewPrep"

# Reviews are also filed here by problem name, so you can read back across
# attempts without digging through timestamped session folders.
REVIEWS_DIR = BASE_DIR / "reviews"

REVIEW_PROMPT_TEMPLATE = """\
# DSA Mock Interview Review

You are an experienced technical interviewer at a product-based company \
(FAANG-adjacent bar). Review this mock interview session and give **blunt, \
specific, interview-style feedback** — no generic encouragement.

**Total session length: {duration}** (all timestamps below are measured from \
the start of the recording).

## Files in this session folder
- `recording.mov` — full screen + audio recording
- `transcript.txt` — auto-transcribed speech, one `[MM:SS] ...` line per \
spoken segment (may contain errors, use judgment). The timestamps are real: \
a gap between one line's timestamp and the next is **actual silence**.
- `code_ocr.txt` — OCR'd code from screen captures spread evenly across the \
whole session, each block headed with the time it was taken (may contain OCR \
errors — infer intent, don't nitpick obvious OCR artifacts like misread \
brackets or 'l' vs '1'). The **last** block is the closest thing to their \
final code; earlier blocks show how it got there.
- `frame_*_at_MM-SS.png` — the raw screenshots those blocks came from, named \
by capture time, for visual reference if the OCR text is unclear

## What to evaluate

1. **Explanation quality**
   - Did they clarify the problem / ask clarifying questions before jumping in?
   - Did they state a brute-force approach before optimizing, or jump straight \
to the optimal one without narrating the reasoning?
   - Did they explain time/space complexity, and was the explanation correct?
   - Structure: was there a clear "here's my plan" before coding, or did \
narration happen reactively while typing?

2. **Speaking efficiency** — use the transcript timestamps, don't eyeball this
   - Filler words, rambling vs. concise articulation
   - **Silences**: find the gaps between consecutive `[MM:SS]` lines. Call out \
any stretch over ~20s with its timestamp — that is dead air an interviewer \
sits through. Distinguish quiet-while-typing from quiet-while-stuck by \
checking what the surrounding frames show.
   - **Pacing**: against the total session length, how long did they spend \
before writing the first line of code, and how long debugging at the end? \
Compare that to a ~30-45 min slot.
   - Did they think out loud in a way an interviewer could actually follow?

3. **Coding style** (from code_ocr.txt / frames — judge the *final* state \
from the last blocks, and use the earlier ones to see how the code evolved)
   - Variable naming, function decomposition, readability
   - Did the code match what was verbally explained?
   - Edge case handling — were edge cases mentioned AND coded, or just mentioned?

4. **Correctness & approach**
   - Is the final approach actually correct and optimal for the problem?
   - Any bugs visible in the OCR'd code?
   - Would this pass in a real interview, or would it get dinged — and specifically why?

## Output format

Be direct. Treat this like a real debrief after a rejected interview loop,
not a pep talk. Cite `[MM:SS]` timestamps whenever you reference a specific
moment, so each point can be checked against the recording.

**Write your full review as a single JSON object to a file named \
`review_data.json` in this same folder ({session_dir}).** Don't print it in \
chat — save it as that file so it becomes a permanent record. It gets \
rendered into a rich HTML report by a template, so the JSON is the only \
output that matters. Output *only* the JSON object in that file — no \
markdown fences, no commentary.

Match this exact schema:

```json
{{
  "slug": "kebab-case-problem-name, e.g. two-sum, lru-cache, merge-k-sorted-lists (use \\"unknown\\" if you can't tell)",
  "title": "Human-readable problem name, e.g. \\"Two Sum (LeetCode 1)\\"",
  "session_length_sec": 1059,
  "session_length_label": "17:39",
  "hire_signal": "one of: strong_no, no, borderline, yes, strong_yes",
  "verdict": "one blunt sentence — the single most important takeaway, readable at a glance",
  "scores": {{
    "explanation_quality": 0,
    "speaking_efficiency": 0,
    "coding_style": 0,
    "correctness": 0
  }},
  "top_fixes": [
    {{"headline": "short imperative headline", "detail": "1-3 sentences, cite [MM:SS] timestamps"}}
  ],
  "what_was_good": ["short bullet, honest — omit entirely if nothing stands out"],
  "key_moments": [
    {{"time_sec": 257, "label": "short label e.g. 'bug introduced'", "type": "one of: bug, good, info"}}
  ],
  "silences": [
    {{"start_sec": 737, "end_sec": 850}}
  ],
  "sections": {{
    "explanation_quality": {{"score": 0, "body_html": "1-3 short paragraphs of analysis, may use <p>, <strong>, <em>, <code>, <ul><li> only — cite [MM:SS] timestamps"}},
    "speaking_efficiency": {{"score": 0, "body_html": "..."}},
    "coding_style": {{"score": 0, "body_html": "..."}},
    "correctness": {{"score": 0, "body_html": "..."}}
  }},
  "final_code": {{"language": "e.g. cpp, python", "code": "the final submitted code, verbatim from the last OCR block", "frame_time": "MM:SS it was captured at"}},
  "rewritten_explanation": {{
    "weak_moment_time": "MM:SS",
    "original": "the actual weak/rambling thing they said, quoted from the transcript",
    "better": "how a strong candidate would have said the same thing"
  }}
}}
```

Notes on filling it in:
- `silences` should list every gap over ~15s between transcript lines (this \
drives the timeline chart) — compute these from the actual `[MM:SS]` gaps, \
don't estimate.
- `key_moments` should mark the first line of code, where a bug was \
introduced/found, and any other moment worth flagging on the timeline (3-6 \
entries is plenty).
- `body_html` fields are inserted directly into the page — keep them to the \
listed tags only, no `<script>`/`<style>`, and escape any literal `<`/`>` \
that isn't one of those tags.
- All 4 scores are 0-10 integers and should match the `sections` scores.

---

## transcript.txt (embedded below for convenience)

{transcript}

---

## code_ocr.txt (embedded below for convenience)

{code_ocr}
"""


def _run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def probe_duration(path: Path):
    """Length of a media file in seconds, or None if ffprobe can't say.

    Callers must handle None: a recording whose moov atom never got written
    (ffmpeg killed mid-session) probes as unknown, and that shouldn't take
    the whole review down with it.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _fmt_clock(seconds: float) -> str:
    """Seconds -> MM:SS (or H:MM:SS past an hour). Used for transcript
    timestamps, frame labels and the session length in the prompt, so they
    all read the same way and Claude can cross-reference them."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def extract_audio(video_path: Path, audio_out: Path):
    """Fallback path: pull audio out of the video and scrub it hard.

    Only used when the native voice-processing capture wasn't available, in
    which case the audio is ffmpeg's raw mic grab -- noisy, and often clipped
    because avfoundation applies no gain control. Cuts sub-100Hz rumble,
    denoises steady hiss/fan noise, then evens out levels. This is damage
    control after the fact, not a substitute for capturing cleanly.
    """
    _run([
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-af", "highpass=f=100,afftdn=nf=-25,dynaudnorm=f=150:g=15",
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(audio_out),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def prepare_audio(session_dir: Path, audio_out: Path):
    """Produce the 16kHz mono WAV Whisper wants.

    Prefers mic.wav from the native voice-processing recorder -- Apple's
    noise suppression and AGC have already done the heavy lifting there, so
    it only needs a level pass and a resample. Denoising it again would just
    chew on speech.
    """
    mic_path = session_dir / "mic.wav"
    if mic_path.exists() and mic_path.stat().st_size > 1024:
        _run([
            "ffmpeg", "-y", "-i", str(mic_path),
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            str(audio_out),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    extract_audio(session_dir / "recording.mov", audio_out)


def mux_clean_audio(session_dir: Path):
    """Put the clean mic track into recording.mov so the saved video is
    actually watchable later. No-op when there's no separate mic track."""
    mic_path = session_dir / "mic.wav"
    video_path = session_dir / "recording.mov"
    if not (mic_path.exists() and video_path.exists()):
        return
    if mic_path.stat().st_size <= 1024:
        return

    merged = session_dir / "_merged.mov"
    try:
        _run([
            "ffmpeg", "-y", "-i", str(video_path), "-i", str(mic_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-shortest", str(merged),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        merged.unlink(missing_ok=True)
        return  # keep the silent video rather than losing the recording
    merged.replace(video_path)


def transcribe_audio(audio_path: Path, out_txt: Path):
    """Uses local Whisper. First run downloads the model (one-time, free).

    Writes one `[MM:SS] text` line per Whisper segment rather than a single
    undifferentiated blob. The review rubric grades pacing and long silences,
    which is unanswerable from untimed text -- with timings, gaps between
    segments are visible and feedback can cite a moment you can scrub to in
    recording.mov. Falls back to the plain text if a model build ever returns
    no segments.
    """
    import whisper
    from config import WHISPER_MODEL

    model = whisper.load_model(WHISPER_MODEL)
    # This always runs on CPU (no CUDA on macOS); fp16=True is Whisper's
    # default and silently falls back with a warning every single run.
    result = model.transcribe(str(audio_path), fp16=False)

    lines = [
        f"[{_fmt_clock(seg['start'])}] {seg['text'].strip()}"
        for seg in result.get("segments") or []
        if seg.get("text", "").strip()
    ]
    text = "\n".join(lines) if lines else result["text"].strip()
    out_txt.write_text(text)
    return text


def frame_interval(duration, max_frames: int, floor_seconds: float) -> float:
    """Spacing that fits `max_frames` grabs across the whole recording.

    A fixed interval silently truncates coverage: `-frames:v` stops the output
    after N frames, so 12 frames every 20s only ever reached 3m40s into the
    session no matter how long it ran. A 22-minute session was reviewed on its
    first four minutes -- the last frame Claude saw was an empty function stub,
    and it graded "coding style" and "bugs in the code" on that.

    So the interval is derived from the duration instead, spanning first frame
    at 0 to last frame at the end. `floor_seconds` (FRAME_INTERVAL_SECONDS)
    is the minimum spacing, so short sessions keep the old cadence and just
    produce fewer frames rather than near-duplicates.
    """
    if not duration or duration <= 0 or max_frames < 2:
        return floor_seconds
    # -1s so the last frame lands just inside the recording rather than at the
    # exact end, where there may be no frame to sample.
    return max(floor_seconds, (duration - 1) / (max_frames - 1))


def extract_frames(video_path: Path, session_dir: Path):
    """Grab screenshots spread across the session. Returns [(path, seconds)].

    Frames are renamed to carry their capture time (`frame_007_at_12-30.png`)
    so a review that points at one is pointing at a place you can scrub to,
    and so code state can be lined up against the timestamped transcript.
    """
    from config import FRAME_INTERVAL_SECONDS, MAX_FRAMES_TO_OCR

    # Frames from an earlier run were spaced differently and would otherwise
    # survive alongside the new ones and get OCR'd with wrong timestamps.
    for stale in session_dir.glob("frame_*.png"):
        stale.unlink()

    duration = probe_duration(video_path)
    interval = frame_interval(duration, MAX_FRAMES_TO_OCR, FRAME_INTERVAL_SECONDS)

    pattern = session_dir / "frame_%03d.png"
    _run([
        "ffmpeg", "-y", "-i", str(video_path),
        # Passed as a decimal rate rather than "1/{interval}": the interval is
        # fractional and ffmpeg's ratio parsing on that is not worth relying on.
        # round=up matters: with the default rounding the final slot lands past
        # the last source frame and is dropped, losing the finished code -- the
        # one frame the review most needs. (Measured on a 22:05 recording: 11
        # frames ending at 20:03 by default, 12 ending at 22:04 with round=up.)
        "-vf", f"fps={1.0 / interval:.9f}:round=up",
        "-frames:v", str(MAX_FRAMES_TO_OCR),
        str(pattern),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    frames = []
    for path in sorted(session_dir.glob("frame_[0-9][0-9][0-9].png")):
        # fps= emits frames at k*interval, so the index gives the source time.
        at = (int(path.stem.split("_")[1]) - 1) * interval
        stamped = path.with_name(f"{path.stem}_at_{_fmt_clock(at).replace(':', '-')}.png")
        path.rename(stamped)
        frames.append((stamped, at))
    return frames


def ocr_frames(frames, out_txt: Path):
    """OCR each (path, seconds) frame into one timestamped text file."""
    import pytesseract
    from PIL import Image

    chunks = []
    for path, at in frames:
        try:
            text = pytesseract.image_to_string(Image.open(path))
        except Exception as e:
            text = f"[OCR failed for {path.name}: {e}]"
        chunks.append(f"--- {path.name} @ {_fmt_clock(at)} ---\n{text.strip()}\n")

    combined = "\n".join(chunks)
    out_txt.write_text(combined)
    return combined


def run_claude_code_review(session_dir: Path, prompt_path: Path) -> Path:
    """Feeds the prompt to Claude Code CLI non-interactively. Claude Code
    itself writes review_data.json into session_dir (instructed to do so in
    the prompt). Returns the expected path to review_data.json.

    Raises subprocess.TimeoutExpired or subprocess.CalledProcessError on
    failure — caller should catch and surface these to the user rather than
    silently failing.
    """
    from config import (
        CLAUDE_CLI_PATH,
        CLAUDE_CLI_SKIP_PERMISSIONS,
        CLAUDE_CLI_TIMEOUT_SECONDS,
    )

    data_path = session_dir / "review_data.json"
    prompt_text = prompt_path.read_text()

    cmd = [CLAUDE_CLI_PATH, "--print", prompt_text]
    if CLAUDE_CLI_SKIP_PERMISSIONS:
        cmd.insert(1, "--dangerously-skip-permissions")

    # Run with session_dir as cwd so relative file references in the prompt
    # (frame_*.png etc.) resolve correctly, and so Claude Code's own file
    # writes default to landing here.
    result = subprocess.run(
        cmd,
        cwd=str(session_dir),
        capture_output=True,
        text=True,
        timeout=CLAUDE_CLI_TIMEOUT_SECONDS,
    )

    # Some Claude Code CLI versions print the response to stdout rather than
    # writing the file directly, even when told to. Belt-and-suspenders: if
    # review_data.json wasn't created but we got stdout back, pull the JSON
    # object out of it ourselves.
    if not data_path.exists() and result.stdout.strip():
        extracted = _extract_json_object(result.stdout.strip())
        if extracted:
            data_path.write_text(extracted)

    if result.returncode != 0 and not data_path.exists():
        raise RuntimeError(
            f"Claude Code CLI failed (exit {result.returncode}): {result.stderr[:500]}"
        )

    return data_path


def _extract_json_object(text: str) -> str:
    """Pull the first top-level {...} object out of Claude's stdout.

    Used when Claude prints the JSON (possibly wrapped in markdown fences or
    an apology) instead of writing review_data.json directly.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    candidate = text[start:end + 1]
    try:
        json.loads(candidate)
    except json.JSONDecodeError:
        return ""
    return candidate


def _sanitize_slug(raw: str) -> str:
    """Turn whatever Claude wrote into a safe filename fragment."""
    slug = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    return slug[:60] or "unknown"


def render_review_html(review_data: dict, out_path: Path, nav=None) -> Path:
    """Render review_data.json into a self-contained HTML report using
    review_template.html, embedding the data inline so the file opens
    standalone (no server, no network) straight from the filesystem.

    `nav`, if given, is a dict of {index_uri, prev: {uri,title}|None,
    next: {uri,title}|None} rendered as an in-page nav bar (see
    _nav_for()). Hrefs are absolute file:// URIs, since this page can live
    in either the session folder or reviews/ and both need to resolve.
    """
    template_path = Path(__file__).parent / "review_template.html"
    template = template_path.read_text()
    payload = dict(review_data)
    if nav:
        payload["_nav"] = nav
    data_json = json.dumps(payload, indent=2)
    html = template.replace("/*__REVIEW_DATA__*/", data_json)
    out_path.write_text(html)
    return out_path


_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")


def _review_sort_key(path: Path) -> str:
    """Sort published reviews by the session timestamp in their filename
    (`review-<slug>-<timestamp>.json`), not by the slug prefix, so reviews of
    different problems still land in chronological order.
    """
    match = _TS_RE.search(path.stem)
    return match.group(0) if match else path.stem


def _published_reviews():
    """All published review JSON files in REVIEWS_DIR, oldest first."""
    return sorted(REVIEWS_DIR.glob("review-*.json"), key=_review_sort_key)


def _nav_for(json_path: Path, all_jsons) -> dict:
    """Build the {index, prev, next} nav dict for one entry in all_jsons."""
    idx = all_jsons.index(json_path)
    prev_path = all_jsons[idx - 1] if idx > 0 else None
    next_path = all_jsons[idx + 1] if idx < len(all_jsons) - 1 else None

    def entry(p):
        if p is None:
            return None
        d = json.loads(p.read_text())
        return {
            "uri": p.with_suffix(".html").resolve().as_uri(),
            "title": d.get("title") or d.get("slug") or "Review",
        }

    return {
        "index_uri": (REVIEWS_DIR / "index.html").resolve().as_uri(),
        "prev": entry(prev_path),
        "next": entry(next_path),
    }


def render_reviews_index(all_jsons=None) -> Path:
    """(Re)generate reviews/index.html, the browsable list of every past
    review, using reviews_index_template.html. Called whenever a new review
    is published so the index never goes stale.
    """
    if all_jsons is None:
        all_jsons = _published_reviews()

    cards = []
    for p in reversed(all_jsons):  # newest first
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        match = _TS_RE.search(p.stem)
        cards.append({
            "title": d.get("title") or d.get("slug") or "Review",
            "href": p.with_suffix(".html").name,
            "date": match.group(0).replace("_", " ") if match else "",
            "hire_signal": d.get("hire_signal"),
            "verdict": d.get("verdict", ""),
            "scores": d.get("scores", {}),
        })

    template_path = Path(__file__).parent / "reviews_index_template.html"
    template = template_path.read_text()
    html = template.replace("/*__REVIEWS_DATA__*/", json.dumps(cards, indent=2))
    out_path = REVIEWS_DIR / "index.html"
    out_path.write_text(html)
    return out_path


def publish_review(session_dir: Path, review_data: dict, slug: str, session_html_path=None):
    """File the review under ~/DSAInterviewPrep/reviews/, named by problem,
    and wire it into the cross-review navigation: an in-page nav bar
    (previous/next review + a link back to the full index) on every review,
    and a regenerated reviews/index.html listing all of them.

    The session folder keeps a copy too (rendered with the same nav) so
    review.html is fully navigable no matter which copy you open. Returns the
    published html path.
    """
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    # session_dir.name is already YYYY-MM-DD_HH-MM-SS, which keeps repeat
    # attempts at the same problem from overwriting each other.
    base = f"review-{slug}-{session_dir.name}"
    published_html = REVIEWS_DIR / f"{base}.html"
    published_json = REVIEWS_DIR / f"{base}.json"
    published_json.write_text(json.dumps(review_data, indent=2))

    all_jsons = _published_reviews()
    nav = _nav_for(published_json, all_jsons)
    render_review_html(review_data, published_html, nav=nav)
    if session_html_path is not None:
        render_review_html(review_data, session_html_path, nav=nav)

    # Backfill the previously-newest review's "next" link now that this one
    # exists — otherwise it would dead-end going forward until re-processed.
    idx = all_jsons.index(published_json)
    if idx > 0:
        prev_json = all_jsons[idx - 1]
        prev_data = json.loads(prev_json.read_text())
        prev_nav = _nav_for(prev_json, all_jsons)
        render_review_html(prev_data, prev_json.with_suffix(".html"), nav=prev_nav)

    render_reviews_index(all_jsons)
    return published_html


# Phase labels, in order. PHASE_COUNT drives the pie-chart fraction shown in
# the menu bar, so adding a phase here automatically updates the UI.
PHASES = [
    "Preparing audio",
    "Merging audio + video",
    "Transcribing speech",
    "Capturing screenshots",
    "Reading code (OCR)",
    "Claude reviewing",
    "Done",
]
PHASE_COUNT = len(PHASES) - 1  # "Done" isn't work


def _nonempty(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def process_session(session_dir: Path, progress=None) -> Path:
    """Runs the pipeline for one session folder and returns review.html's
    path.

    **Resumable.** Every step is skipped when its output already exists, so a
    session that died partway -- most commonly the Claude review timing out
    after Whisper already spent minutes transcribing -- can be re-run without
    redoing the expensive local work.

    `progress` is an optional callable(step_index, total, label) used to drive
    the menu-bar progress indicator. It's called from whatever thread runs the
    pipeline, so the UI side must marshal to the main thread itself.
    """
    video_path = session_dir / "recording.mov"
    audio_path = session_dir / "audio.wav"
    transcript_path = session_dir / "transcript.txt"
    ocr_path = session_dir / "code_ocr.txt"
    prompt_path = session_dir / "review_prompt.md"
    html_path = session_dir / "review.html"

    def report(step):
        if progress:
            progress(step, PHASE_COUNT, PHASES[step])

    have_transcript = _nonempty(transcript_path)

    # 1-3. Audio prep, mux and transcription. Whisper is the slow one, so if a
    # transcript already exists the whole audio branch is skipped.
    if have_transcript:
        report(2)
        transcript_text = transcript_path.read_text(errors="replace")
    else:
        report(0)
        prepare_audio(session_dir, audio_path)
        report(1)
        mux_clean_audio(session_dir)
        report(2)
        transcript_text = transcribe_audio(audio_path, transcript_path)

    # 4-5. Frames + OCR, skipped if we already have OCR text.
    if _nonempty(ocr_path):
        ocr_text = ocr_path.read_text(errors="replace")
    else:
        report(3)
        frames = extract_frames(video_path, session_dir)
        report(4)
        ocr_text = ocr_frames(frames, ocr_path)

    # Intermediates are safe to drop once folded into recording.mov.
    audio_path.unlink(missing_ok=True)
    (session_dir / "mic.wav").unlink(missing_ok=True)

    # 6. Write the review prompt with content embedded inline. The duration is
    # probed here rather than passed down from the recorder so a reprocessed
    # session gets it too.
    duration = probe_duration(video_path)
    prompt = REVIEW_PROMPT_TEMPLATE.format(
        session_dir=str(session_dir),
        duration=_fmt_clock(duration) if duration else "unknown",
        transcript=transcript_text.strip() or "[No speech detected]",
        code_ocr=ocr_text.strip() or "[No frames captured]",
    )
    prompt_path.write_text(prompt)

    # 7. Hand off to Claude Code CLI — it writes review_data.json itself
    report(5)
    data_path = run_claude_code_review(session_dir, prompt_path)

    if not _nonempty(data_path):
        raise RuntimeError(
            "Claude Code CLI did not produce review_data.json — check "
            f"{session_dir / 'review_prompt.md'} and re-run the CLI manually."
        )
    review_data = json.loads(data_path.read_text())

    # 8. File it under reviews/ (named by the problem Claude identified),
    # which also renders both the session and reviews/ copies with
    # cross-review navigation and refreshes reviews/index.html.
    slug = _sanitize_slug(review_data.get("slug", "unknown"))
    publish_review(session_dir, review_data, slug, session_html_path=html_path)

    report(6)
    return html_path


def unprocessed_sessions(sessions_dir: Path):
    """Session folders that have a recording but no review yet, newest last."""
    if not sessions_dir.exists():
        return []
    return sorted(
        d for d in sessions_dir.iterdir()
        if d.is_dir()
        and (d / "recording.mov").exists()
        and not _nonempty(d / "review.html")
    )
