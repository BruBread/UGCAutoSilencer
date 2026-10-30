"""Silence Cutter: find silences in a video, review them in the browser, render the cut."""
import json
import subprocess
from pathlib import Path

import numpy as np

# Tuning (seconds unless noted)
FRAME = 0.02            # loudness frame
THRESH_FRACTION = 0.70  # silence line, 0 = noise floor, 1 = speech level
MIN_GAP = 0.25          # shorter silences stay in
MIN_BLIP = 0.10         # shorter sounds are ignored
PAD = 0.08              # kept around each spoken piece
FADE = 0.01             # audio fade at each cut
MIN_CONTRAST_DB = 6     # below this the video is all speech or all silence
ABS_SILENCE_DB = -45    # a video whose loud parts sit below this is all silence


def loudness_db(samples, sr):
    hop = int(sr * FRAME)
    n = len(samples) // hop
    frames = samples[: n * hop].reshape(n, hop).astype(np.float64)
    return 10 * np.log10((frames ** 2).mean(axis=1) + 1e-12)


def _runs(mask):
    """(start, end) frame index pairs of True runs."""
    edges = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.nonzero(edges == 1)[0], np.nonzero(edges == -1)[0]))


def detect_pieces(db, duration):
    """Kept (spoken) pieces in seconds, sorted and non-overlapping."""
    if len(db) == 0:
        return []
    floor, speech = np.percentile(db, 10), np.percentile(db, 90)
    if speech - floor < MIN_CONTRAST_DB:
        return [] if speech < ABS_SILENCE_DB else [(0.0, duration)]
    runs = _runs(db > floor + THRESH_FRACTION * (speech - floor))
    merged = []
    for a, b in runs:
        if merged and (a - merged[-1][1]) * FRAME < MIN_GAP:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    pieces = []
    for a, b in merged:
        if (b - a) * FRAME < MIN_BLIP:
            continue
        s, e = max(0.0, a * FRAME - PAD), min(duration, b * FRAME + PAD)
        if pieces and s <= pieces[-1][1]:
            pieces[-1] = (pieces[-1][0], e)
        else:
            pieces.append((s, e))
    return pieces


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"Could not read this file as a video:\n{r.stderr.strip()}")
    j = json.loads(r.stdout)
    video = next((s for s in j["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise SystemExit("This file has no video stream.")
    return {
        "duration": float(j["format"]["duration"]),
        "fps": video.get("r_frame_rate", "30/1"),
        "hdr": video.get("color_transfer") in ("arib-std-b67", "smpte2084"),
        "transfer": video.get("color_transfer"),
        "has_audio": any(s["codec_type"] == "audio" for s in j["streams"]),
    }


def decode_audio(path, sr=16000, band=True):
    """Mono float32 samples. band=True keeps only the voice range, dropping hum and rumble."""
    args = ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr)]
    if band:
        args += ["-af", "highpass=f=100,lowpass=f=4000"]
    r = subprocess.run(args + ["-f", "f32le", "-"], capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"Could not read the audio:\n{r.stderr.decode(errors='replace').strip()}")
    return np.frombuffer(r.stdout, np.float32)


def analyze(path):
    info = probe(path)
    if not info["has_audio"]:
        raise SystemExit("This video has no audio, so there is nothing to detect silence in.")
    db = loudness_db(decode_audio(path), 16000)
    return info, db, detect_pieces(db, info["duration"])


def next_output_path(src):
    src = Path(src)
    out = src.with_name(f"{src.stem}_edited.mp4")
    n = 2
    while out.exists():
        out = src.with_name(f"{src.stem}_edited_{n}.mp4")
        n += 1
    return out
