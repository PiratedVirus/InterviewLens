#!/bin/bash
# Launch the DSA Interview Prep Recorder.
#
# Uses the venv's interpreter directly, so it works regardless of whether
# you've run `source venv/bin/activate` -- the usual cause of
# "ModuleNotFoundError: No module named 'rumps'".
#
# Run this from the same Terminal you granted Screen Recording and
# Microphone permission to; macOS attributes those to the launching app.
set -e
cd "$(dirname "$0")"

if [ ! -x venv/bin/python ]; then
    echo "No virtualenv found. Run ./setup.sh first."
    exit 1
fi

if [ ! -x bin/voicerecorder ]; then
    echo "Note: bin/voicerecorder is missing, so the app will fall back to"
    echo "ffmpeg's raw (noisier) mic capture. Run ./setup.sh to build it."
fi

exec venv/bin/python menubar_app.py
