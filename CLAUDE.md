# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local macOS menu-bar app that records screen + audio while the user talks through a DSA
(data structures & algorithms) practice problem, then locally transcribes/OCRs the session and
hands it to the Claude Code CLI to generate a graded review. Personal-use tool, not
production-grade or meant for distribution — see README.md's "Known rough edges" section before
suggesting hardening work that isn't asked for.

Everything runs locally and free: Whisper (transcription) and Tesseract (OCR) both run on-device.
The only "cloud" step is the final review, which goes through the user's existing Claude Code CLI
subscription — there are no API keys and no billing in this codebase.

## Setup & running

```bash
./setup.sh                          # one-time: brew install ffmpeg tesseract, create venv, pip install
source venv/bin/activate
python menubar_app.py               # launches the menu-bar app
```

There is no test suite or linter. The one build step is the native mic helper, compiled by
`setup.sh` (see "Audio capture" below); rebuild it with:
```bash
clang -fobjc-arc -O2 -o bin/voicerecorder native/voicerecorder.m \
  -framework AVFoundation -framework AudioToolbox -framework Foundation
```
Manual verification is done by actually running the app (see README's setup section for
required `config.py` device indices and macOS Screen Recording / Microphone permissions).

To debug the Claude Code CLI hand-off manually, `cd` into a session folder and re-run the exact
command the pipeline uses:
```bash
cd ~/DSAInterviewPrep/sessions/<timestamp>
claude --print "$(cat review_prompt.md)"
```

## Architecture

Three files, single linear pipeline, no classes/abstractions beyond the one `rumps.App` subclass:

- **`menubar_app.py`** — the `rumps` menu-bar UI. Owns the ffmpeg recording subprocess lifecycle
  (start via `Popen`, stop by sending `q` on stdin like pressing it in a terminal) and the native
  mic helper alongside it, plus the timer-driven menu-bar icon states. On stop, runs
  `pipeline.process_session()` on a background thread (so the menu stays responsive) and opens the
  resulting `review.md`.
- **`pipeline.py`** — the actual processing pipeline, called with a session directory and run
  synchronously, in order: `prepare_audio` → `transcribe_audio` (Whisper) → `extract_frames` →
  `ocr_frames` (Tesseract) → `mux_clean_audio` → build `review_prompt.md` from
  `REVIEW_PROMPT_TEMPLATE` →
  `run_claude_code_review` (shells out to `claude --print`, cwd'd into the session folder).
  `BASE_DIR = ~/DSAInterviewPrep` is defined here and imported by `menubar_app.py`.
- **`config.py`** — all user-tunable knobs in one place: device *names* (see below), Whisper model
  size, frame-capture interval/limit, native-capture toggle, and the Claude CLI
  path/timeout/skip-permissions flag.
- **`devices.py`** — resolves avfoundation device indices by name at record time. Indices are
  positional and shift when hardware connects: an iPhone connecting via Continuity Camera pushes
  "Capture screen 0" from index 1 to 3, so a hardcoded index silently targets the iPhone camera and
  ffmpeg dies on the unsupported framerate. Never reintroduce hardcoded indices. Run
  `python devices.py` to print the current list.

### Audio capture is deliberately not ffmpeg's job

The mic is recorded by `bin/voicerecorder`, a small Objective-C helper built
from `native/voicerecorder.m`, not by ffmpeg. ffmpeg's `avfoundation` input
takes the raw mic stream (measured: -33dB noise floor, clipping at +3dBFS);
the helper routes through macOS's Voice Processing I/O audio unit, which is
where Apple's noise suppression / echo cancellation / AGC live (measured:
-60dB noise floor, no clipping). When native capture is active, ffmpeg records
**video only** and `pipeline.mux_clean_audio()` folds `mic.wav` back into
`recording.mov` afterwards.

Non-obvious constraints in that helper, all learned the hard way:
- **VPIO is duplex.** Enabling only the input bus fails `AudioUnitInitialize`
  with -10875 and delivers zero buffers. The output bus must be enabled and
  fed silence (`SilenceCB`).
- `AVAudioEngine.setVoiceProcessingEnabled:` hits the same wall and fails
  *silently* (starts fine, never calls the tap), which is why this uses the
  AudioUnit API directly.
- The main thread must not park in `dispatch_main()` or callbacks never fire.
- The built-in mic sits at 96kHz while output runs at 48kHz;
  `align_input_sample_rate()` pins the input device to 48kHz.
- The helper stops when its **stdin closes**. Launch it with a real stdin pipe
  (`subprocess.PIPE`) — hand it `/dev/null` and it exits after ~0.1s.

`config.py:USE_NATIVE_VOICE_CAPTURE` turns this off; the app then falls back to
the old combined ffmpeg capture, and `pipeline.extract_audio()`'s aggressive
denoise chain exists only for that fallback path.

### Session data flow

Every recording creates `~/DSAInterviewPrep/sessions/<timestamp>/`, and all pipeline steps read/write
files within that one folder: `recording.mov` (video-only while recording) + `mic.wav` → `audio.wav`
(deleted after transcription) + `transcript.txt`, `frame_*_at_MM-SS.png` → `code_ocr.txt`, then `review_prompt.md` (the full prompt, with
transcript and OCR text embedded inline) is fed to `claude --print`, which is instructed by the
prompt itself to write `review.md` back into that same folder. `run_claude_code_review` also
writes `review.md` from captured stdout as a fallback, since different Claude Code CLI versions
vary in whether `--print` writes files directly.

### Resumability and progress

`process_session(session_dir, progress=None)` skips any step whose output already exists
(`transcript.txt` short-circuits audio prep + mux + Whisper; `code_ocr.txt` short-circuits frame
extraction + OCR). This exists because the Claude review is the most likely step to fail — it
timed out at the 240s default on a 22-minute session, after Whisper had already done the slow
work. `unprocessed_sessions()` finds folders with a `recording.mov` but no non-empty `review.md`.

The `progress` callback is `(step_index, total, label)` over `PHASES`, and is invoked **from the
pipeline thread**. `menubar_app` only records it into `self._progress`; a `rumps.Timer` renders it
on the main thread, because AppKit is not thread-safe. Icon frames are `icons/pie_00..12.png` and
the fraction is mapped to the nearest frame, so changing `PHASES` needs no icon regeneration.

Menu items whose titles change at runtime (the status line, the reprocess item) are held as Python
attributes rather than looked up via `self.menu["..."]` — rumps keys menu items by title, so
retitling breaks key lookup.

### Review filing

`publish_review()` copies each finished review to
`~/DSAInterviewPrep/reviews/review-<slug>-<session-timestamp>.md`. The slug comes from a
`<!-- question-slug: NAME -->` marker the review prompt asks Claude to emit; `question_slug()`
scans the first 10 lines (not just line 1 — Claude sometimes prefixes a preamble when it can't
write the file directly) and falls back to `unknown`. `_sanitize_slug()` strips it to
`[a-z0-9-]`, so a malformed or path-like slug can't escape the reviews directory. `strip_preamble()`
removes anything above the marker, which is how the apology text gets discarded when Claude Code
can't write the file itself and the stdout fallback saves it instead. The session
folder keeps the authoritative `review.md`; reviews/ is a flat index.

### Everything the review sees is timestamped

The rubric grades pacing, silences and *final* code, so both inputs carry time:

- `transcribe_audio` writes one `[MM:SS] text` line per Whisper segment, not one blob. Gaps
  between consecutive lines are the only evidence of dead air that exists — plain text made the
  "long silences" and "pacing" criteria unanswerable, and Claude was guessing at them.
- `extract_frames` derives its spacing from the recording's duration
  (`frame_interval()`) instead of using a fixed one. `-frames:v` caps the *output*, so the old
  fixed `fps=1/20` + 12 frames stopped at 3m40s regardless of session length: a 22-minute session
  was reviewed on its first four minutes, and the last frame Claude saw was an empty function
  stub it then graded for style and bugs. `FRAME_INTERVAL_SECONDS` is now a floor for short
  sessions, not a cadence.
- That fps filter needs **`round=up`**. With default rounding the final slot falls past the last
  source frame and is silently dropped — which loses the finished code, the single most important
  frame. (Measured on a 22:05 recording: 11 frames ending at 20:03 by default, 12 ending at 22:04
  with `round=up`.)
- Frames are renamed to `frame_007_at_12-02.png` after extraction and `code_ocr.txt` blocks are
  headed `--- name @ MM:SS ---`, so code state lines up against the transcript and any moment the
  review cites can be scrubbed to in `recording.mov`. `ocr_frames()` therefore takes
  `(path, seconds)` tuples, not bare paths.

### Key behavioral notes for anyone touching the pipeline

- ffmpeg's stderr goes to `<session>/ffmpeg.log` and `start_recording` checks the process is still
  alive ~1.5s after launch. Both exist because a bad device index made ffmpeg exit instantly while
  the mic kept recording, producing an audio-only session that wasn't noticed until review time —
  don't route that stderr back to DEVNULL.
- `run_claude_code_review` runs with `cwd=session_dir` specifically so relative file references in
  the prompt (`frame_*.png`, etc.) resolve, and so Claude Code's own writes default there.
- `CLAUDE_CLI_SKIP_PERMISSIONS` in `config.py` controls whether the CLI call includes
  `--dangerously-skip-permissions`. Since each session is a brand-new folder, leaving this `False`
  means a permission prompt per session; this is a deliberate user-facing tradeoff, not a bug.
- The review prompt (`REVIEW_PROMPT_TEMPLATE` in `pipeline.py`) is the actual interview rubric —
  explanation quality, speaking efficiency, coding style, correctness — and is the place to edit if
  the user wants to change grading criteria, not the Python control flow around it.
