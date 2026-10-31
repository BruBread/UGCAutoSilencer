"""Silence Cutter: find silences in a video, review them in the browser, render the cut."""
import json
import os
import subprocess
import tempfile
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


LUT_SIZE = 33


def _write_tonemap_lut(transfer, path):
    """Bake the HDR->SDR conversion into a 3D LUT. Same look as running zscale+tonemap per frame
    (checked on the iPhone sample: closest to CapCut's own export), about 4x less CPU."""
    g = np.linspace(0, 1, LUT_SIZE)
    b, gr, r = np.meshgrid(g, g, g, indexing="ij")  # .cube order: red changes fastest
    lattice = (np.stack([r.ravel(), gr.ravel(), b.ravel()], 1) * 65535).round().astype("<u2")
    chain = (f"setparams=color_trc={transfer}:color_primaries=bt2020:colorspace=gbr:range=pc,"
             "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
             "zscale=t=bt709,format=gbrpf32le")
    res = subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb48le",
                          "-s", f"{LUT_SIZE * LUT_SIZE}x{LUT_SIZE}", "-i", "-", "-vf", chain,
                          "-f", "rawvideo", "-pix_fmt", "gbrpf32le", "-"],
                         input=lattice.tobytes(), capture_output=True)
    if res.returncode != 0:
        raise RuntimeError("Could not build the HDR conversion:\n" + res.stderr.decode(errors="replace")[-500:])
    gp, bp, rp = np.frombuffer(res.stdout, "<f4").reshape(3, -1)
    rgb = np.stack([rp, gp, bp], 1).clip(0, 1)
    rows = "\n".join(f"{x:.6f} {y:.6f} {z:.6f}" for x, y, z in rgb)
    Path(path).write_text(f"LUT_3D_SIZE {LUT_SIZE}\n{rows}\n")


SDR_TAGS = ",setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
HDR_TO_SDR = ("scale=in_color_matrix=bt2020:in_range=tv,format=gbrp16le,lut3d=file=tonemap.cube:interp=tetrahedral,"
              "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p")


def _filter_graph(pieces, info):
    """Input i is piece i (seeked with -ss/-to), so only kept parts get decoded."""
    lines, labels = [], []
    for i, (a, b) in enumerate(pieces):
        fade_out = max(0.0, b - a - FADE)
        lines.append(f"[{i}:v]setpts=PTS-STARTPTS[v{i}];")
        lines.append(f"[{i}:a]asetpts=PTS-STARTPTS,afade=t=in:d={FADE},afade=t=out:st={fade_out:.4f}:d={FADE}[a{i}];")
        labels.append(f"[v{i}][a{i}]")
    # Tag the result as SDR BT.709 so players don't treat converted iPhone footage as HDR
    video_out = (HDR_TO_SDR if info["hdr"] else "format=yuv420p") + SDR_TAGS
    lines.append(f"{''.join(labels)}concat=n={len(pieces)}:v=1:a=1[cv][outa];[cv]{video_out}[outv]")
    return "\n".join(lines)


ENCODERS = [
    ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "18", "-qp_p", "18"],
    ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"],  # when the AMD encoder is unavailable
]


def render(src, info, pieces, out, on_progress):
    """Cut `pieces` out of `src` into `out`. Writes to a .part file first so a failure leaves nothing behind."""
    out = Path(out).resolve()
    part = out.with_suffix(".part.mp4")
    total = sum(b - a for a, b in pieces)
    # ponytail: one input per piece; a few hundred pieces would hit the Windows command-line limit
    inputs = [x for a, b in pieces for x in ("-ss", f"{a:.4f}", "-to", f"{b:.4f}", "-i", str(Path(src).resolve()))]
    with tempfile.TemporaryDirectory() as work:  # ffmpeg runs here so filter files need no path escaping
        (Path(work) / "graph.txt").write_text(_filter_graph(pieces, info))
        if info["hdr"]:
            _write_tonemap_lut(info["transfer"], Path(work) / "tonemap.cube")
        err = ""
        for enc in ENCODERS:
            cmd = ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex_script", "graph.txt",
                   "-map", "[outv]", "-map", "[outa]", *enc, "-pix_fmt", "yuv420p",
                   "-fps_mode", "cfr", "-r", info["fps"], "-c:a", "aac", "-b:a", "192k",
                   "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(part)]
            p = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for line in p.stdout:
                if line.startswith("out_time_us=") and total > 0:
                    us = line.split("=", 1)[1].strip()
                    if us.isdigit():
                        on_progress(min(1.0, int(us) / 1e6 / total))
            err = p.stderr.read()
            if p.wait() == 0:
                os.replace(part, out)
                on_progress(1.0)
                return
            part.unlink(missing_ok=True)
            if not Path(src).exists():
                break
        raise RuntimeError("FFmpeg failed:\n" + "\n".join(err.strip().splitlines()[-5:]))


def next_output_path(src):
    src = Path(src)
    out = src.with_name(f"{src.stem}_edited.mp4")
    n = 2
    while out.exists():
        out = src.with_name(f"{src.stem}_edited_{n}.mp4")
        n += 1
    return out
