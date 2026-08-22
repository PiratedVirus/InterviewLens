#!/bin/bash
# One-time setup for DSA Interview Prep Recorder (macOS only)
set -e

echo "== DSA Interview Prep Recorder — Setup =="

# 1. Check for Homebrew
if ! command -v brew &> /dev/null; then
    echo "Homebrew not found. Install it first: https://brew.sh"
    exit 1
fi

# 2. Install ffmpeg and tesseract (both free, local, open-source)
echo "-- Installing ffmpeg + tesseract via brew (skips if already installed) --"
brew install ffmpeg tesseract

# 3. Build the native mic recorder (macOS Voice Processing I/O -- this is what
#    gives QuickTime-quality audio instead of ffmpeg's noisy raw capture).
echo "-- Building native voice recorder --"
mkdir -p bin
if clang -fobjc-arc -O2 -o bin/voicerecorder native/voicerecorder.m \
        -framework AVFoundation -framework AudioToolbox -framework Foundation; then
    echo "   built bin/voicerecorder"
else
    echo "   WARNING: build failed -- the app will fall back to ffmpeg's raw"
    echo "   mic capture, which is noticeably noisier. Xcode Command Line"
    echo "   Tools are required: xcode-select --install"
fi

# 4. Create Python virtual environment
echo "-- Creating virtual environment --"
python3 -m venv venv
source venv/bin/activate

# 5. Install Python dependencies
echo "-- Installing Python dependencies (this may take a few minutes) --"
pip install --upgrade pip
# openai-whisper 20231117 has a legacy setup.py that imports pkg_resources,
# which modern setuptools no longer provides under build isolation. Pin an
# older setuptools and build against it directly.
pip install "setuptools<81" wheel
pip install --no-build-isolation -r requirements.txt

echo ""
echo "== Setup complete =="
echo ""
echo "NEXT STEPS (manual, required):"
echo "1. Run this to find your device indices:"
echo "     ffmpeg -f avfoundation -list_devices true -i \"\""
echo "2. Open config.py and set SCREEN_DEVICE_INDEX and AUDIO_DEVICE_INDEX"
echo "   based on what you see."
echo "3. First run will also ask for Screen Recording + Microphone permissions"
echo "   in System Settings > Privacy & Security. Grant them to your terminal"
echo "   app (or Python, depending on how macOS prompts you)."
echo "4. Run the app with:"
echo "     source venv/bin/activate && python menubar_app.py"
echo ""
echo "Optional (system audio, e.g. to capture interview platform audio too):"
echo "   brew install blackhole-2ch"
echo "   Then create a Multi-Output Device in Audio MIDI Setup combining"
echo "   BlackHole + your speakers, and select BlackHole as AUDIO_DEVICE_INDEX."
