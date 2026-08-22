"""
avfoundation device lookup.

ffmpeg's avfoundation device indices are positional and **shift whenever a
device connects or disconnects** -- plugging in an iPhone (Continuity Camera)
inserts entries and renumbers everything after them, so a hardcoded
"screen is index 1" silently becomes "index 1 is the iPhone camera" and the
recording fails or records the wrong thing.

So indices are resolved by name at record time instead.
"""

import re
import subprocess

_DEVICE_LINE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")


def list_devices():
    """Returns (video, audio) lists of (index, name) tuples."""
    # This command always "fails" (there's no real input to open); the device
    # listing goes to stderr regardless.
    proc = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    video, audio, current = [], [], None
    for line in proc.stderr.splitlines():
        if "AVFoundation video devices:" in line:
            current = video
            continue
        if "AVFoundation audio devices:" in line:
            current = audio
            continue
        if current is None:
            continue
        # Strip the "[AVFoundation indev @ 0x...] " prefix before matching, so
        # the address doesn't get mistaken for a device index.
        stripped = re.sub(r"^\[AVFoundation indev @ 0x[0-9a-f]+\]\s*", "", line)
        m = _DEVICE_LINE.match(stripped)
        if m:
            current.append((int(m.group(1)), m.group(2)))
    return video, audio


def find_device(devices, name_contains):
    """First device whose name contains `name_contains` (case-insensitive)."""
    needle = name_contains.lower()
    for index, name in devices:
        if needle in name.lower():
            return index
    return None


def resolve_screen_index(configured_index, name_contains):
    """Explicit index wins; otherwise look the screen up by name."""
    if configured_index is not None:
        return configured_index
    video, _ = list_devices()
    return find_device(video, name_contains)


def resolve_audio_index(configured_index, name_contains):
    if configured_index is not None:
        return configured_index
    _, audio = list_devices()
    return find_device(audio, name_contains)


if __name__ == "__main__":
    video, audio = list_devices()
    print("video devices:")
    for i, n in video:
        print(f"  [{i}] {n}")
    print("audio devices:")
    for i, n in audio:
        print(f"  [{i}] {n}")
