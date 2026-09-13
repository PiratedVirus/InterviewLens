#!/usr/bin/env python3
"""
DSA Interview Prep Recorder — Menu Bar App
--------------------------------------------
Click Start in the menu bar -> records screen + mic (+ system audio if configured).
Click Stop -> transcribes audio locally (Whisper), OCRs code frames locally (Tesseract),
writes a session folder, and runs it through the Claude Code CLI headlessly --
no Claude Desktop or Cowork step. review.html opens automatically when it's ready.

Everything runs locally. No API keys. No cloud calls except the one-time Whisper
model download (first run only).
"""

import os
import subprocess
import threading
import time
import signal
from datetime import datetime
from pathlib import Path

import rumps

from pipeline import (
    process_session, unprocessed_sessions, PHASES, PHASE_COUNT,
    BASE_DIR, REVIEWS_DIR,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Monochrome menu-bar icons (see generate_icons.py). Loaded as macOS
# "template" images, so the OS recolors them to match the current menu bar
# (light/dark) automatically -- the same mechanism the built-in Wi-Fi/
# Bluetooth icons use.
ICONS_DIR = Path(__file__).parent / "icons"
ICON_IDLE = str(ICONS_DIR / "idle.png")
ICON_REC_FRAMES = [str(ICONS_DIR / "rec_a.png"), str(ICONS_DIR / "rec_b.png")]
ICON_PROC_FRAMES = [str(ICONS_DIR / f"proc_{i}.png") for i in range(4)]
# Pie-chart progress frames, 0/12 .. 12/12 filled.
ICON_PIE_STEPS = 12
ICON_PIE_FRAMES = [str(ICONS_DIR / f"pie_{i:02d}.png") for i in range(ICON_PIE_STEPS + 1)]

# Devices are resolved by name at record time (see devices.py) because
# avfoundation indices shift when hardware connects or disconnects.
from config import (
    SCREEN_DEVICE_INDEX,
    AUDIO_DEVICE_INDEX,
    SCREEN_DEVICE_NAME,
    AUDIO_DEVICE_NAME,
    RECORD_VIDEO,
    USE_NATIVE_VOICE_CAPTURE,
)
import devices

# Native mic capture helper (see native/voicerecorder.m). Built by setup.sh.
VOICE_RECORDER = Path(__file__).parent / "bin" / "voicerecorder"

# Records the live ffmpeg PID for the duration of a recording. macOS only
# lets one process hold the screen-capture stream at a time: if this app
# crashes or a stop hangs and ffmpeg is left running, every later recording
# silently gets zero video frames (ScreenCaptureKit reports "connection
# INTERRUPTED" to the *new* ffmpeg instead of erroring loudly) while audio
# keeps recording fine via the separate native mic path -- the mismatch is
# what makes it look like a processing bug instead of a stuck process. This
# file survives app restarts, so start_recording can detect and kill a
# leftover process from a previous crash before it can block a new one.
FFMPEG_PID_FILE = BASE_DIR / ".ffmpeg.pid"


def native_voice_capture_available():
    return USE_NATIVE_VOICE_CAPTURE and VOICE_RECORDER.exists() and os.access(VOICE_RECORDER, os.X_OK)


def notify(title, subtitle, message):
    """Best-effort desktop notification -- never raises.

    rumps.notification() raises RuntimeError unless the app is a bundled .app
    with a CFBundleIdentifier, and this runs as a plain script. That mattered:
    the call sat directly before the line that starts the processing thread,
    so every single session stopped cleanly and then silently did nothing.
    A cosmetic notification must never sit in the way of the actual work.
    """
    try:
        rumps.notification(title, subtitle, message)
        return
    except Exception:
        pass
    try:
        def esc(s):
            return str(s).replace("\\", "").replace('"', "'")
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{esc(message)}" with title "{esc(title)}" '
             f'subtitle "{esc(subtitle)}"'],
            check=False, capture_output=True, timeout=10,
        )
    except Exception:
        pass


class DSARecorderApp(rumps.App):
    def __init__(self):
        super().__init__("DSA Recorder", icon=ICON_IDLE, template=True, quit_button="Quit")
        self.recording = False
        self.ffmpeg_proc = None
        self.ffmpeg_log = None
        self.audio_proc = None
        self.use_native_audio = False
        self.session_dir = None
        self.start_time = None

        self._rec_frame_idx = 0
        self._rec_timer = rumps.Timer(self._pulse_recording_icon, 0.6)
        # Progress is written by the pipeline thread and rendered on the main
        # thread by this timer -- AppKit isn't thread-safe.
        self._progress = None
        self._proc_timer = rumps.Timer(self._render_progress, 0.4)

        # Informational, non-clickable: shows the current phase while working.
        self.status_item = rumps.MenuItem("Idle")
        self.status_item.set_callback(None)

        # Held by reference, not looked up by title -- its title changes to
        # show the pending count, which would break key-based lookup.
        self.reprocess_item = rumps.MenuItem(
            "Reprocess Unfinished Session", callback=self.reprocess_unfinished
        )

        self.menu = [
            self.status_item,
            None,
            "Start Recording",
            "Stop Recording",
            None,
            "Browse All Reviews",
            "Open Reviews Folder",
            "Open Sessions Folder",
            "Show Last Review",
            self.reprocess_item,
        ]
        self.menu["Stop Recording"].set_callback(None)  # disabled until recording
        self._refresh_reprocess_item()

    # -----------------------------------------------------------------
    def _pulse_recording_icon(self, _timer):
        self._rec_frame_idx = 1 - self._rec_frame_idx
        self.icon = ICON_REC_FRAMES[self._rec_frame_idx]

    def _render_progress(self, _timer):
        """Main-thread UI update for whatever the pipeline last reported."""
        if not self._progress:
            return
        step, total, label = self._progress
        fraction = min(step / total, 1.0) if total else 0.0
        self.icon = ICON_PIE_FRAMES[round(fraction * ICON_PIE_STEPS)]
        self.status_item.title = f"{label}…  ({step + 1}/{total})" if step < total \
            else f"{label}  ({total}/{total})"

    def _on_progress(self, step, total, label):
        """Called from the pipeline thread; just records state."""
        self._progress = (step, total, label)

    def _set_status(self, text):
        self.status_item.title = text

    def _refresh_reprocess_item(self):
        """Enable the reprocess action only when there's something to resume."""
        pending = unprocessed_sessions(SESSIONS_DIR)
        if pending:
            self.reprocess_item.set_callback(self.reprocess_unfinished)
            self.reprocess_item.title = f"Reprocess Unfinished Session ({len(pending)})"
        else:
            self.reprocess_item.set_callback(None)
            self.reprocess_item.title = "Reprocess Unfinished Session"

    # -----------------------------------------------------------------
    def _start_native_audio(self):
        """Start the voice-processing mic recorder. Returns True once it has
        confirmed it's actually capturing."""
        mic_path = self.session_dir / "mic.wav"
        try:
            self.audio_proc = subprocess.Popen(
                [str(VOICE_RECORDER), str(mic_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError as e:
            rumps.alert(f"Could not start mic recorder: {e}")
            return False

        # It prints READY only after the audio unit is actually running, so a
        # permissions or device failure surfaces here instead of as a silent
        # empty file discovered at review time.
        ready = self.audio_proc.stdout.readline().decode(errors="replace").strip()
        if ready != "READY":
            err = self.audio_proc.stderr.read().decode(errors="replace").strip()
            self.audio_proc = None
            rumps.alert(f"Mic recorder failed to start.\n\n{err[:300]}")
            return False
        return True

    @staticmethod
    def _reap_stale_ffmpeg():
        """Kill and clear any ffmpeg left running by a previous crashed/hung
        instance of this app, identified via FFMPEG_PID_FILE. Safe to call
        unconditionally at the start of every recording."""
        if not FFMPEG_PID_FILE.exists():
            return
        try:
            pid = int(FFMPEG_PID_FILE.read_text().strip())
            # Confirm it's actually still our ffmpeg before killing -- the
            # PID could have been reused by an unrelated process by now.
            comm = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if comm.endswith("ffmpeg"):
                os.kill(pid, signal.SIGKILL)
                notify(
                    "DSA Recorder", "Cleaned up a stuck recording",
                    "A previous session's ffmpeg was still running and would "
                    "have blocked this recording's screen capture."
                )
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
        FFMPEG_PID_FILE.unlink(missing_ok=True)

    def _kill_ffmpeg(self):
        if not self.ffmpeg_proc:
            return
        try:
            self.ffmpeg_proc.communicate(input=b"q", timeout=10)
        except Exception:
            self.ffmpeg_proc.kill()
        self.ffmpeg_proc = None
        self._close_ffmpeg_log()
        FFMPEG_PID_FILE.unlink(missing_ok=True)

    def _close_ffmpeg_log(self):
        if getattr(self, "ffmpeg_log", None):
            try:
                self.ffmpeg_log.close()
            except Exception:
                pass
            self.ffmpeg_log = None

    def _stop_native_audio(self):
        if not self.audio_proc:
            return
        try:
            # Closing stdin is the helper's clean-stop signal; it finalizes the
            # WAV header on its way out.
            self.audio_proc.stdin.close()
            self.audio_proc.wait(timeout=15)
        except Exception:
            self.audio_proc.terminate()
            try:
                self.audio_proc.wait(timeout=5)
            except Exception:
                self.audio_proc.kill()
        self.audio_proc = None

    # -----------------------------------------------------------------
    @rumps.clicked("Start Recording")
    def start_recording(self, _):
        if self.recording:
            rumps.alert("Already recording.")
            return

        self._reap_stale_ffmpeg()

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_dir = SESSIONS_DIR / ts
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.start_time = time.time()

        video_path = self.session_dir / "recording.mov"
        self.use_native_audio = native_voice_capture_available()

        # Resolved fresh every recording: avfoundation indices move when
        # devices come and go.
        screen_index = devices.resolve_screen_index(SCREEN_DEVICE_INDEX, SCREEN_DEVICE_NAME)
        if screen_index is None:
            rumps.alert(
                "Screen capture device not found",
                f"No avfoundation video device matching '{SCREEN_DEVICE_NAME}'.\n\n"
                "Run 'python devices.py' to list devices, then set "
                "SCREEN_DEVICE_NAME or SCREEN_DEVICE_INDEX in config.py."
            )
            return

        # With native capture, ffmpeg records video only and the mic is handled
        # by the voice-processing helper; otherwise fall back to avfoundation's
        # combined "SCREEN:AUDIO" input.
        if self.use_native_audio:
            av_input = f"{screen_index}"
        else:
            audio_index = devices.resolve_audio_index(AUDIO_DEVICE_INDEX, AUDIO_DEVICE_NAME)
            if audio_index is None:
                rumps.alert(
                    "Microphone not found",
                    f"No avfoundation audio device matching '{AUDIO_DEVICE_NAME}'.\n\n"
                    "Run 'python devices.py' to list devices."
                )
                return
            av_input = f"{screen_index}:{audio_index}"
        cmd = [
            "ffmpeg", "-y",
            "-f", "avfoundation",
            "-framerate", "10" if RECORD_VIDEO else "1",
            "-i", av_input,
            "-vcodec", "libx264", "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            str(video_path),
        ]

        # Keep ffmpeg's stderr: it's the only place device/framerate errors
        # show up, and discarding it turns a failed recording into a silent
        # one you don't discover until review time.
        self.ffmpeg_log = open(self.session_dir / "ffmpeg.log", "wb")
        try:
            self.ffmpeg_proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.ffmpeg_log
            )
        except FileNotFoundError:
            rumps.alert("ffmpeg not found. Install it first: brew install ffmpeg")
            return

        # ffmpeg exits almost immediately on a bad device or unsupported
        # framerate, so catch that now rather than after a whole session.
        time.sleep(1.5)
        if self.ffmpeg_proc.poll() is not None:
            self.ffmpeg_log.close()
            log_text = (self.session_dir / "ffmpeg.log").read_text(errors="replace")
            detail = "\n".join(
                l for l in log_text.splitlines()
                if any(k in l for k in ("not supported", "Error", "error", "Supported modes"))
            )[:400]
            rumps.alert("Screen recording failed to start", detail or log_text[-400:])
            return

        FFMPEG_PID_FILE.write_text(str(self.ffmpeg_proc.pid))

        if self.use_native_audio and not self._start_native_audio():
            # Mic helper failed -- stop the video-only ffmpeg and bail rather
            # than silently recording a session with no audio at all.
            self._kill_ffmpeg()
            return

        self.recording = True
        self._rec_frame_idx = 0
        self._rec_timer.start()
        self.menu["Start Recording"].set_callback(None)
        self.menu["Stop Recording"].set_callback(self.stop_recording)
        notify("DSA Recorder", "Recording started", str(self.session_dir.name))

    # -----------------------------------------------------------------
    def stop_recording(self, _):
        if not self.recording:
            return

        # Grab the session dir and proc now, and immediately flip state so a
        # second click (or any exception below) can't re-enter this with a
        # half-torn-down ffmpeg_proc -- that's what previously produced a
        # crash loop where the menu stayed stuck on "recording".
        proc = self.ffmpeg_proc
        self.ffmpeg_proc = None
        self.recording = False
        self._rec_timer.stop()
        self.menu["Start Recording"].set_callback(self.start_recording)
        self.menu["Stop Recording"].set_callback(None)

        self._stop_ffmpeg(proc)
        self._close_ffmpeg_log()
        FFMPEG_PID_FILE.unlink(missing_ok=True)
        self._stop_native_audio()

        # Start the real work FIRST. Anything cosmetic that raises must not be
        # able to strand a finished recording unprocessed (see notify()).
        self._begin_processing(self.session_dir)
        notify(
            "DSA Recorder", "Processing...",
            "Transcribing + OCR locally, then running Claude Code review"
        )

    @staticmethod
    def _stop_ffmpeg(proc):
        """Best-effort, never-raises shutdown: graceful quit, then SIGINT,
        then SIGKILL. Any single stage timing out must fall through to the
        next rather than propagate and leave the process (and app state)
        stranded."""
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.communicate(input=b"q", timeout=10)
            return
        except Exception:
            pass
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=10)
            return
        except Exception:
            pass
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    # -----------------------------------------------------------------
    def _begin_processing(self, session_dir):
        self._progress = (0, PHASE_COUNT, PHASES[0])
        self._proc_timer.start()
        threading.Thread(
            target=self._process_and_review, args=(session_dir,), daemon=True
        ).start()

    def _process_and_review(self, session_dir):
        try:
            review_path = process_session(session_dir, progress=self._on_progress)
        except subprocess.TimeoutExpired:
            self._finish_processing("Review timed out")
            notify(
                "DSA Recorder", "Claude Code timed out",
                "Increase CLAUDE_CLI_TIMEOUT_SECONDS in config.py, then use "
                "'Reprocess Unfinished Session' — the transcript and OCR are kept."
            )
            return
        except Exception as e:
            self._finish_processing("Processing failed")
            notify("DSA Recorder", "Processing failed", str(e)[:200])
            return

        self._finish_processing("Idle")

        if not review_path.exists():
            notify(
                "DSA Recorder", "No review.html found",
                "Claude Code ran but didn't write the file. Check the session folder."
            )
            subprocess.run(["open", str(session_dir)])
            return

        notify("DSA Recorder", "Review ready", review_path.name)

        # Open review.html in the default browser
        subprocess.run(["open", str(review_path)])

    def _finish_processing(self, status):
        """Stop the progress timer, reset the icon, refresh the menu."""
        self._progress = None
        self._proc_timer.stop()
        self._rec_timer.stop()
        self.icon = ICON_IDLE
        self._set_status(status)
        self._refresh_reprocess_item()

    # -----------------------------------------------------------------
    def reprocess_unfinished(self, _):
        """Resume the newest session that has a recording but no review.

        Steps whose output already exists are skipped, so a session that died
        at the Claude review step doesn't re-run Whisper.
        """
        if self.recording:
            rumps.alert("Still recording — stop the current session first.")
            return
        if self._progress is not None:
            rumps.alert("Already processing a session.")
            return

        pending = unprocessed_sessions(SESSIONS_DIR)
        if not pending:
            rumps.alert("Nothing to reprocess.", "Every session already has a review.")
            return

        session_dir = pending[-1]  # newest
        self._begin_processing(session_dir)
        notify("DSA Recorder", "Reprocessing", session_dir.name)

    # -----------------------------------------------------------------
    @rumps.clicked("Browse All Reviews")
    def browse_all_reviews(self, _):
        index_path = REVIEWS_DIR / "index.html"
        if not index_path.exists():
            rumps.alert("No completed reviews yet.")
            return
        subprocess.run(["open", str(index_path)])

    @rumps.clicked("Open Reviews Folder")
    def open_reviews_folder(self, _):
        REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(REVIEWS_DIR)])

    @rumps.clicked("Open Sessions Folder")
    def open_sessions_folder(self, _):
        subprocess.run(["open", str(SESSIONS_DIR)])

    @rumps.clicked("Show Last Review")
    def show_last_review(self, _):
        # reviews/ filenames end with the session timestamp, so a plain sort
        # puts the newest last.
        published = sorted(REVIEWS_DIR.glob("review-*.html"))
        if published:
            subprocess.run(["open", str(published[-1])])
            return
        sessions = sorted(SESSIONS_DIR.glob("*/review.html"), reverse=True)
        if not sessions:
            rumps.alert("No completed reviews yet.")
            return
        subprocess.run(["open", str(sessions[0])])


if __name__ == "__main__":
    DSARecorderApp().run()
