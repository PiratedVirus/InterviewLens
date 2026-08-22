"""
Config — EDIT THESE before first run.

To find your device indices, run this once in Terminal:

    ffmpeg -f avfoundation -list_devices true -i ""

It will print something like:
    [AVFoundation indev @ 0x...] AVFoundation video devices:
    [0] FaceTime HD Camera
    [1] Capture screen 0
    [AVFoundation indev @ 0x...] AVFoundation audio devices:
    [0] MacBook Pro Microphone
    [1] BlackHole 2ch          <- only if you installed BlackHole for system audio

Set SCREEN_DEVICE_INDEX to the "Capture screen 0" number.
Set AUDIO_DEVICE_INDEX to your microphone (or BlackHole if you want system audio too).
"""

# Devices are looked up by NAME at record time, because avfoundation indices
# shift whenever a device connects or disconnects. (Plugging in an iPhone adds
# "iPhone Camera" / "iPhone Desk View Camera" at indices 1-2 and pushes
# "Capture screen 0" from 1 to 3 -- a hardcoded index then silently points at
# the iPhone camera and recording fails.)
#
# Run `python devices.py` to see the current list.
SCREEN_DEVICE_NAME = "Capture screen"
AUDIO_DEVICE_NAME = "MacBook Air Microphone"

# Hard overrides. Leave as None to use the name lookup above; set an integer
# only if you want to force a specific index.
SCREEN_DEVICE_INDEX = None
AUDIO_DEVICE_INDEX = None

# If True, records video at 10fps (bigger files, lets Claude literally see your
# screen via extracted frames). If False, records audio-only + periodic
# screenshots (smaller, faster, usually enough for DSA review purposes).
RECORD_VIDEO = True

# Capture the mic through macOS's Voice Processing I/O unit (bin/voicerecorder)
# instead of letting ffmpeg grab the raw mic stream. This is what QuickTime and
# FaceTime use: Apple's noise suppression, echo cancellation and automatic gain
# control. Measured on this machine it dropped the noise floor from -33dB to
# -60dB and removed the clipping the raw capture had.
#
# When True, ffmpeg records video only and the mic is recorded separately to
# mic.wav, which is then muxed back into recording.mov. If the helper binary is
# missing (run ./setup.sh to build it) the app automatically falls back to the
# old combined ffmpeg capture.
USE_NATIVE_VOICE_CAPTURE = True

# Whisper model size: "tiny", "base", "small", "medium", "large"
# tiny/base = fast, less accurate. small = good balance for interview speech.
WHISPER_MODEL = "small"

# Minimum spacing between screenshot frames (seconds). Frames are spread
# evenly across the whole recording so the final code is always captured, so
# this is a floor, not a fixed cadence: a long session stretches the spacing
# to fit MAX_FRAMES_TO_OCR grabs across it, while a short one falls back to
# this interval and simply yields fewer frames.
FRAME_INTERVAL_SECONDS = 20

# Max number of frames to OCR per session (keeps processing time sane). This
# is the resolution of the code timeline: 12 frames over a 40-minute session
# is a snapshot every ~3.5 minutes.
MAX_FRAMES_TO_OCR = 12

# ---------------------------------------------------------------------------
# Claude Code CLI review settings
# ---------------------------------------------------------------------------

# Path to the claude CLI binary. Usually just "claude" if it's on your PATH
# (check with `which claude`). Set a full path here if not.
CLAUDE_CLI_PATH = "claude"

# If True, runs with --dangerously-skip-permissions so it writes review.md
# with zero interactive prompts (fully one-click, unattended).
# If False, you'll see Claude Code ask for permission the first time it
# writes a file in a new folder — safer default, one extra click per NEW
# session folder location the first time.
#
# Since every session is a brand-new timestamped folder, skip-permissions
# is what actually makes this "one click" in practice. Only enable this if
# you're comfortable with Claude Code writing files unattended inside
# ~/DSAInterviewPrep — it will never touch anything outside the folder you
# point it at.
CLAUDE_CLI_SKIP_PERMISSIONS = True

# How long to wait for Claude Code to finish the review before giving up
# (seconds). Reviews involve reading transcript + OCR + several images, so
# give it a couple minutes of headroom.
CLAUDE_CLI_TIMEOUT_SECONDS = 480
