# DSA Interview Prep Recorder

A local, free, menu-bar app for macOS that records your screen + audio while
you talk through a DSA problem, then locally transcribes and OCRs the session
and automatically runs it through the Claude Code CLI for review — no Claude
Desktop or Cowork step required.

**Cost: $0.** Everything (recording, transcription, OCR) runs on your Mac.
No API keys, no subscriptions beyond the Claude Pro/Max/Team plan you already
use for Claude Code.

**Scope: personal use only.** This is a rough tool for your own interview
prep, not something meant to be distributed or hardened for other users.

---

## What it does

1. Click **Start Recording** in the menu bar → records your screen + mic.
2. Talk through and code a DSA problem like you would in a real interview.
3. Click **Stop Recording** →
   - Extracts audio → transcribes it locally with **Whisper** (OpenAI's
     open-source speech-to-text model, runs on your Mac, no internet needed
     after the first model download)
   - Grabs periodic screenshots from the recording → OCRs them locally with
     **Tesseract** to pull out your code
   - Writes everything into a timestamped session folder, and files a copy
     of the review under `~/DSAInterviewPrep/reviews/` named by problem
   - Feeds it all to the **Claude Code CLI** (`claude --print`), which reads
     the transcript, OCR'd code, and screenshots, judges the session against
     a built-in interview rubric, and writes the result as `review.md` in
     that same session folder
   - Opens `review.md` automatically in your default markdown viewer

No clipboard pasting, no manually opening Claude Desktop. One click to stop
→ a finished, saved review file. Every past session's review stays on disk
permanently, so you build a searchable history of feedback across your whole
prep — see "Reviewing past sessions" below for how reviews are filed by
problem name so you can track a recurring weakness over time.

---

## One-time setup

```bash
cd dsa_interview_app
chmod +x setup.sh
./setup.sh
```

This installs `ffmpeg` and `tesseract` via Homebrew (both free, open-source,
run locally) and sets up a Python virtual environment.

### Devices (no manual setup needed)

Devices are resolved **by name** at record time, so there's normally nothing to
configure. To see what's currently attached:

```bash
python devices.py
```

**Why by name:** avfoundation indices are positional and shift whenever hardware
connects or disconnects. Plugging in an iPhone (Continuity Camera) inserts
"iPhone Camera" and "iPhone Desk View Camera" at indices 1–2 and pushes
"Capture screen 0" from index 1 to 3. With a hardcoded index the app then points
at the iPhone camera, which rejects the 10fps setting, and recording fails.

If the lookup ever picks the wrong device, override it in `config.py`:
```python
SCREEN_DEVICE_NAME = "Capture screen"          # matched case-insensitively
SCREEN_DEVICE_INDEX = None                     # or an int to force an index
```

### macOS permissions

The first time you record, macOS will prompt for **Screen Recording** and
**Microphone** access. Grant these in System Settings → Privacy & Security.
You may need to restart the app once after granting.

### (Optional) Capture system audio too

If you want to also capture audio from an interview platform (e.g. practicing
against a recorded question read aloud), install
[BlackHole](https://github.com/ExistentialAudio/BlackHole):

```bash
brew install blackhole-2ch
```

Then in Audio MIDI Setup, create a Multi-Output Device combining BlackHole +
your speakers, and set `AUDIO_DEVICE_INDEX` to BlackHole's index. Otherwise,
just your microphone is captured, which is enough for talking through your
own explanation.

---

## Running the app

```bash
./run.sh
```

`run.sh` uses the venv's interpreter directly, so it works whether or not you've
run `source venv/bin/activate` — skipping that step is the usual cause of
`ModuleNotFoundError: No module named 'rumps'`.

A monochrome ring icon appears in your menu bar (it follows the menu bar's
light/dark tone). It pulses as a filled dot while recording, and spins while
the session is being processed.

To have it always available, you can later wrap this in a `.app` bundle with
`py2app`, but for personal use just running the script when you want to
practice is simplest.

---

## One-time Claude Code CLI setup

This uses the `claude` command-line tool (a different surface from Claude
Desktop / Cowork), running on your existing Pro/Max/Team subscription — no
separate API key or billing.

1. Install Claude Code if you haven't already:
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```
   (or follow Anthropic's current install docs if this has changed — check
   `docs.claude.com` for the latest command)
2. Run `claude` once in a terminal and sign in. Confirm it works:
   ```bash
   claude --print "say hello"
   ```
3. Check it's on your PATH: `which claude`. If `config.py`'s
   `CLAUDE_CLI_PATH` needs a full path instead of just `"claude"`, set it
   there.
4. **Permissions prompt:** the first time Claude Code writes a file inside a
   brand-new folder, it may ask for permission interactively. Since every
   recording session creates a new timestamped folder, this can mean a
   permission prompt on every single session. Two options, both in
   `config.py`:
   - Leave `CLAUDE_CLI_SKIP_PERMISSIONS = False` (default) — safer, but you
     may need to approve the first file write per session in the terminal
     Claude Code opens.
   - Set `CLAUDE_CLI_SKIP_PERMISSIONS = True` — fully unattended, true
     one-click. Only enable this once you trust the flow; it means Claude
     Code writes `review.md` with no confirmation step. It still only ever
     touches the session folder it's pointed at.

That's it — no Cowork folder-access setup, no Desktop app interaction
needed. The whole review happens headlessly through the CLI.

### Reviewing past sessions

Reviews are filed twice. The session folder keeps the authoritative copy
next to the video and transcript:

```
~/DSAInterviewPrep/sessions/<timestamp>/review.md
```

and a flat, greppable index is kept by problem name:

```
~/DSAInterviewPrep/reviews/
├── review-two-sum-2026-08-22_15-20-25.md
├── review-two-sum-2026-08-25_09-10-31.md      <- same problem, later attempt
└── review-lru-cache-2026-08-23_11-05-02.md
```

The problem name is inferred by Claude during the review — it writes a
`<!-- question-slug: two-sum -->` marker near the top of `review.md`, which
is an HTML comment so it stays invisible when rendered. If it can't tell what
problem you were solving, the file is named `review-unknown-<timestamp>.md`.
The timestamp is always included, so practising the same problem repeatedly
never overwrites an earlier attempt.

That flat layout makes progress tracking easy:

```bash
ls ~/DSAInterviewPrep/reviews/review-two-sum-*      # every attempt at one problem
grep -l "complexity" ~/DSAInterviewPrep/reviews/*   # a recurring weakness
```

**Open Reviews Folder** and **Show Last Review** in the menu bar jump straight
there.

---

## Known rough edges (this is intentionally not production-grade)

- **Screenshots span the whole session, spread to fit.** `MAX_FRAMES_TO_OCR`
  grabs are spaced across the full recording rather than taken at a fixed
  interval, so the last one always lands on your finished code;
  `FRAME_INTERVAL_SECONDS` is the *minimum* spacing, which only binds on short
  sessions. Frames are named by capture time (`frame_007_at_12-02.png`) and the
  transcript is written as timestamped `[MM:SS]` lines, so the review can cite a
  moment and you can scrub straight to it in `recording.mov` — and so the
  pacing/silence part of the rubric is measured rather than guessed at.
- **OCR accuracy on code is imperfect.** Tesseract will misread some
  brackets, indentation, and similar-looking characters (`l`/`1`/`I`,
  `O`/`0`). The review prompt tells Claude to use judgment about this, but
  for cleaner code review, consider also saving your code to a `.txt`/`.py`
  file in the session folder before stopping the recording — Claude can read
  that directly instead of relying only on OCR.
- **Mic audio goes through macOS Voice Processing, not ffmpeg.** ffmpeg's
  `avfoundation` input grabs the *raw* mic stream: no noise suppression, no
  gain control. On this machine that measured a -33dB noise floor with peaks
  clipping at +3dBFS, which made Whisper produce repetition-loop garbage.
  `bin/voicerecorder` (built from `native/voicerecorder.m` by `setup.sh`)
  records the mic through Apple's Voice Processing I/O audio unit instead —
  the same path QuickTime and FaceTime use, and the only one eligible for
  the **Voice Isolation** mic mode in Control Center. Measured result:
  noise floor -60dB, peaks -6.6dB, no clipping. Two gotchas worth knowing if
  you ever touch that file: VPIO is *duplex* and delivers nothing at all
  unless the output bus is also enabled (it's fed silence), and it pins the
  input device to 48kHz because the built-in mic defaults to 96kHz.
  Set `USE_NATIVE_VOICE_CAPTURE = False` in `config.py` to go back to the
  raw ffmpeg capture, or run `VR_NO_VP=1 bin/voicerecorder out.wav` to
  record a raw sample for A/B comparison.
- **Whisper transcription takes real time** — a few seconds to a couple
  minutes depending on session length and the model size in `config.py`
  (`small` is a decent speed/accuracy tradeoff; `tiny`/`base` are faster but
  less accurate on technical vocabulary).
- **Sessions resume; nothing is lost to a timeout.** Every pipeline step is
  skipped when its output already exists, so a session that dies partway
  through can be picked up where it stopped. This matters most when the Claude
  review times out *after* Whisper has already spent minutes transcribing:
  bump `CLAUDE_CLI_TIMEOUT_SECONDS`, then use **Reprocess Unfinished Session**
  from the menu bar and it re-runs only the review. That menu item shows a
  count of resumable sessions and is greyed out when there are none.
- **The menu-bar icon is a pie-chart progress indicator** while processing,
  filling as it moves through the phases (preparing audio → merging → Whisper
  → screenshots → OCR → Claude review). Click the icon and the top line of the
  menu names the current phase, e.g. "Transcribing speech…  (3/6)".
- **Claude Code review also takes real time** — reading a transcript, OCR
  text, and several images isn't instant. `CLAUDE_CLI_TIMEOUT_SECONDS` in
  `config.py` defaults to 4 minutes; bump it up if longer sessions time out.
- **Claude Code CLI versions vary in whether `--print` writes files
  directly or just prints to stdout.** The pipeline handles both cases (it
  writes `review.md` itself from stdout if Claude Code didn't), but if you
  hit issues, run the same command manually in a session folder to see what
  it's actually doing:
  ```bash
  cd ~/DSAInterviewPrep/sessions/<timestamp>
  claude --print "$(cat review_prompt.md)"
  ```
- **First Whisper run downloads a model file** (100MB–1.5GB depending on
  size) — needs internet once, then works offline.
- No video/audio ever leaves your machine except what Claude Code CLI sends
  when it actually runs the review — same as any other content you'd hand
  to Claude.
