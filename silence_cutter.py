"""Silence Cutter: find silences in a video, review them in the browser, render the cut."""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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


# --- review server -------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
RELOAD_GRACE = 5      # seconds a closed page has to come back (a reload) before we cancel
IDLE_TIMEOUT = 180    # no request at all for this long cancels (background tabs ping slowly)


class State:
    def __init__(self, src, info, db, pieces, tmp):
        self.src, self.info, self.db, self.pieces, self.tmp = Path(src), info, db, pieces, Path(tmp)
        self.phase, self.value, self.out, self.error = "review", 0.0, None, None
        self.last_seen, self.bye_at = time.time(), None
        self.lock = threading.Lock()
        if info["hdr"]:
            _write_tonemap_lut(info["transfer"], self.tmp / "tonemap.cube")

    def cancelled(self):
        now = time.time()
        if self.phase != "review":
            return False
        if self.bye_at and self.last_seen <= self.bye_at and now - self.bye_at > RELOAD_GRACE:
            return True
        return now - self.last_seen > IDLE_TIMEOUT

    def video_filter(self, extra):
        return f"{extra},{HDR_TO_SDR}" if self.info["hdr"] else extra

    def frames(self, start, end, n):
        key = f"{start:.3f}_{end:.3f}_{n}"
        folder = self.tmp / "frames" / key
        if not folder.exists():
            folder.mkdir(parents=True)
            dur = max(0.1, end - start)
            subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(self.src),
                            "-vf", self.video_filter(f"fps={n / dur:.4f},scale=-2:160"), "-frames:v", str(n),
                            "-q:v", "5", str(folder / "%02d.jpg")], cwd=self.tmp, capture_output=True)
        return [f"/thumb/{key}/{p.name}" for p in sorted(folder.glob("*.jpg"))]

    def preview(self):
        path = self.tmp / "preview.mp4"
        with self.lock:
            if not path.exists():
                subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.src), "-vf", self.video_filter("scale=-2:540"),
                                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p",
                                "-c:a", "aac", "-movflags", "+faststart", str(path)], cwd=self.tmp, capture_output=True)
        return path

    def save(self, pieces):
        self.phase, self.out = "saving", next_output_path(self.src)

        def work():
            try:
                render(self.src, self.info, pieces, self.out, lambda v: setattr(self, "value", v))
                self.phase = "done"
            except Exception as e:  # shown to the user on the page and in the console
                self.error, self.phase = str(e), "error"

        threading.Thread(target=work, daemon=True).start()


def _valid_pieces(raw, duration):
    try:
        pieces = [(float(a), float(b)) for a, b in raw]
    except (TypeError, ValueError):
        return None
    prev = 0.0
    for a, b in pieces:
        if not (prev - 1e-6 <= a < b <= duration + 0.05):
            return None
        prev = b
    return pieces or None


def make_server(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, ctype="application/json"):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path, ctype):
            size = path.stat().st_size
            start, end = 0, size - 1
            m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if m and (m[1] or m[2]):
                start = int(m[1]) if m[1] else size - int(m[2])
                end = min(int(m[2]), size - 1) if m[1] and m[2] else size - 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def do_GET(self):
            state.last_seen = time.time()
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path == "/":
                    self.send(200, (HERE / "review.html").read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/data":
                    self.send(200, json.dumps({
                        "duration": state.info["duration"], "frame": FRAME, "name": state.src.name,
                        "db": [round(float(x), 1) for x in state.db], "pieces": state.pieces}))
                elif url.path == "/video":
                    self.send_file(state.src, "video/mp4")  # .mov is the same container family; browsers take it as mp4
                elif url.path == "/preview":
                    self.send_file(state.preview(), "video/mp4")
                elif url.path == "/frames":
                    start, end = float(q["start"][0]), float(q["end"][0])
                    self.send(200, json.dumps(state.frames(start, end, max(1, min(30, int(q.get("n", ["12"])[0]))))))
                elif url.path.startswith("/thumb/"):
                    parts = url.path.split("/")
                    if len(parts) != 4 or not re.fullmatch(r"[\d._]+", parts[2]) or not re.fullmatch(r"\d+\.jpg", parts[3]):
                        return self.send(404, "not found", "text/plain")
                    path = state.tmp / "frames" / parts[2] / parts[3]
                    if not path.exists():
                        return self.send(404, "not found", "text/plain")
                    self.send(200, path.read_bytes(), "image/jpeg")
                elif url.path == "/progress":
                    self.send(200, json.dumps({"phase": state.phase, "value": state.value,
                                               "out": str(state.out) if state.out else None, "error": state.error}))
                else:
                    self.send(404, "not found", "text/plain")
            except (ConnectionError, BrokenPipeError):
                pass  # the browser stopped reading, normal while seeking video

        def do_POST(self):
            state.last_seen = time.time()
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/bye":
                state.bye_at = state.last_seen
                return self.send(204, b"")
            if self.path == "/ping":
                return self.send(204, b"")
            if self.path == "/save":
                if state.phase != "review":
                    return self.send(409, "Already saving.", "text/plain")
                try:
                    pieces = _valid_pieces(json.loads(body).get("pieces"), state.info["duration"])
                except (ValueError, AttributeError):
                    pieces = None
                if not pieces:
                    return self.send(400, "The cut list is not valid.", "text/plain")
                state.save(pieces)
                return self.send(200, json.dumps({"out": str(state.out)}))
            self.send(404, "not found", "text/plain")

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def _bar(label, v):
    sys.stdout.write(f"\r  {label:<18} [{'#' * int(v * 30):-<30}] {v:4.0%} ")
    sys.stdout.flush()


def main():
    if len(sys.argv) < 2:
        raise SystemExit('Usage: python silence_cutter.py "path\\to\\video"')
    src = Path(sys.argv[1].strip().strip('"')).resolve()
    if not src.is_file():
        raise SystemExit(f"File not found: {src}")
    print(f"\n  {src.name}\n")
    _bar("Finding silences", 0.1)
    info, db, pieces = analyze(src)
    _bar("Finding silences", 1.0)
    if not pieces:
        raise SystemExit("No speech found in this video, so there is nothing to keep.")
    print(f"\n  Found {len(pieces)} spoken pieces.\n")
    with tempfile.TemporaryDirectory(prefix="silence_cutter_") as tmp:
        state = State(src, info, db, pieces, tmp)
        srv = make_server(state)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/"
        webbrowser.open(url)
        print(f"  Review the cuts in your browser: {url}")
        print("  Closing the browser tab cancels. Nothing is saved until you press save.\n")
        try:
            while True:
                time.sleep(0.3)
                if state.phase == "saving":
                    _bar("Saving", state.value)
                elif state.phase == "done":
                    _bar("Saving", 1.0)
                    print(f"\n\n  Done! Saved to:\n  {state.out}\n")
                    time.sleep(1.5)  # let the page show "Saved"
                    break
                elif state.phase == "error":
                    print(f"\n\n  Saving failed:\n  {state.error}\n")
                    break
                elif state.cancelled():
                    print("  Cancelled. Nothing was saved.\n")
                    break
        except KeyboardInterrupt:
            print("\n  Cancelled. Nothing was saved.\n")
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            print(f"\n  {e.code}\n")
            sys.exit(1)
