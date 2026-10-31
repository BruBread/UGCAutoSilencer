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
from fractions import Fraction
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


ENCODERS = [
    ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "18", "-qp_p", "18"],
    ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"],  # when the AMD encoder is unavailable
]
BACKGROUND = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)  # keeps the review page snappy
STALL = 60  # seconds without progress before an ffmpeg run counts as stuck


def _ffmpeg_with_progress(cmd, cwd, seconds, on_progress, flags=0, keep=lambda p: None):
    """Run ffmpeg with -progress on stdout; return (returncode, stderr). Kills it if it stalls for STALL seconds."""
    with tempfile.TemporaryFile("w+") as err:  # a file, not a pipe: a full, unread stderr pipe freezes ffmpeg
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=err, text=True, creationflags=flags)
        keep(p)
        last, stalled = [time.time(), None], []

        def watch():
            while p.poll() is None:
                if time.time() - last[0] > STALL:
                    stalled.append(True)
                    p.kill()
                    return
                time.sleep(0.5)

        threading.Thread(target=watch, daemon=True).start()
        for line in p.stdout:
            if line.startswith("out_time_us="):
                us = line.split("=", 1)[1].strip()
                if us != last[1]:  # ffmpeg keeps printing reports while stuck, so only a moving time counts
                    last[:] = time.time(), us
                if us.isdigit() and seconds > 0:
                    on_progress(min(1.0, int(us) / 1e6 / seconds))
        code = p.wait()
        err.seek(0)
        return code, (f"FFmpeg stopped responding for {STALL} seconds.\n" if stalled else "") + err.read()


class SegmentCache:
    """Renders each kept piece to its own video-only file in the background while the user reviews.
    Saving then only re-renders pieces that changed and joins the rest without re-encoding.
    Pieces are snapped to whole frames so the joined video and the audio stay in sync."""

    def __init__(self, src, info, folder):
        self.src, self.info, self.dir = Path(src).resolve(), info, Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fps = Fraction(info["fps"])
        self.done, self.wanted = {}, []          # key = (first frame, end frame)
        self.current, self.current_part = None, 0.0
        self.error, self.encoder, self.stopped, self.proc = None, None, False, None
        self.cond = threading.Condition()
        if info["hdr"] and not (self.dir / "tonemap.cube").exists():
            _write_tonemap_lut(info["transfer"], self.dir / "tonemap.cube")
        threading.Thread(target=self._run, daemon=True).start()

    def keys(self, pieces):
        keys = [(round(a * self.fps), round(b * self.fps)) for a, b in pieces]
        return [k for k in keys if k[1] > k[0]]

    def want(self, pieces, reviewed_until=float("inf")):
        """Queue pieces; the ones the user already reviewed (before `reviewed_until`) go first."""
        limit = reviewed_until * self.fps
        with self.cond:
            self.wanted = sorted(self.keys(pieces), key=lambda k: (k[1] >= limit, k[0]))
            self.error = None
            self.cond.notify_all()

    def progress(self, keys):
        with self.cond:
            total = sum(b - a for a, b in keys) or 1
            ready = sum(b - a for a, b in keys if (a, b) in self.done)
            if self.current in keys:
                ready += (self.current[1] - self.current[0]) * self.current_part
            return ready / total

    def status(self, keys):
        with self.cond:
            done = sum(k in self.done for k in keys)
        return f"Rendering piece {min(done + 1, len(keys))} of {len(keys)}"

    def wait(self, keys, on_progress):
        while True:
            with self.cond:
                if self.error:
                    raise RuntimeError(self.error)
                if all(k in self.done for k in keys):
                    return
                self.cond.wait(0.3)
            on_progress(self.progress(keys))

    def stop(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def _run(self):
        while True:
            with self.cond:
                while not self.stopped and not [k for k in self.wanted if k not in self.done]:
                    self.cond.wait()
                if self.stopped:
                    return
                key = next(k for k in self.wanted if k not in self.done)
                self.current, self.current_part = key, 0.0
            try:
                path = self._render(key)
            except Exception as e:  # any failure must reach the waiting save, never leave it hanging
                with self.cond:
                    self.error, self.wanted, self.current = str(e), [], None
                    self.cond.notify_all()
                continue
            with self.cond:
                self.done[key], self.current = path, None
                self.cond.notify_all()

    def _render(self, key):
        first, end = key
        path = self.dir / f"seg_{first}_{end}.mp4"
        part = path.with_suffix(".part.mp4")
        frames = end - first
        convert = (HDR_TO_SDR if self.info["hdr"] else "format=yuv420p") + SDR_TAGS
        err = ""
        # Every segment must use the same encoder to join, so a stall retries with that encoder once
        for enc in [self.encoder] * 2 if self.encoder else ENCODERS:
            cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{float(first / self.fps):.6f}",
                   "-to", f"{float(end / self.fps):.6f}",
                   "-i", str(self.src), "-an", "-vf", f"{convert},tpad=stop_mode=clone:stop=2",
                   "-fps_mode", "cfr", "-r", self.info["fps"], "-frames:v", str(frames), *enc,
                   "-progress", "pipe:1", "-nostats", str(part)]
            code, err = _ffmpeg_with_progress(cmd, self.dir, float(frames / self.fps),
                                              lambda v: setattr(self, "current_part", v),
                                              BACKGROUND, lambda p: setattr(self, "proc", p))
            if code == 0:
                self.encoder = enc
                os.replace(part, path)
                return path
            part.unlink(missing_ok=True)
            if self.stopped or not self.src.exists():
                break
        raise RuntimeError("FFmpeg failed:\n" + "\n".join(err.strip().splitlines()[-5:]))


def render(src, info, pieces, out, on_progress, cache=None, on_step=lambda s: None):
    """Cut `pieces` out of `src` into `out`. Writes to a .part file first so a failure leaves nothing behind."""
    out = Path(out).resolve()
    part = out.with_suffix(".part.mp4")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as own:
        own_cache = cache is None
        if own_cache:
            cache = SegmentCache(src, info, own)
        try:
            keys = cache.keys(pieces)
            if not keys:
                raise RuntimeError("Nothing to save: every piece is shorter than one frame.")
            cache.want(pieces)
            on_step(cache.status(keys))
            cache.wait(keys, lambda v: (on_progress(0.95 * v), on_step(cache.status(keys))))
            on_step("Joining pieces and audio")
            fps = cache.fps
            (cache.dir / "join.txt").write_text("".join(f"file 'seg_{a}_{b}.mp4'\n" for a, b in keys))
            audio = []
            for i, (a, b) in enumerate(keys):
                dur = float((b - a) / fps)
                audio.append(f"[1:a]atrim=start={float(a / fps):.6f}:end={float(b / fps):.6f},asetpts=PTS-STARTPTS,"
                             f"afade=t=in:d={FADE},afade=t=out:st={max(0.0, dur - FADE):.6f}:d={FADE}[a{i}];")
            audio.append("".join(f"[a{i}]" for i in range(len(keys))) + f"concat=n={len(keys)}:v=0:a=1[outa]")
            (cache.dir / "audio.txt").write_text("\n".join(audio))
            code, err = _ffmpeg_with_progress(
                ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "join.txt", "-i", str(cache.src),
                 "-filter_complex_script", "audio.txt", "-map", "0:v", "-map", "[outa]", "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
                 str(part)],
                cache.dir, float(sum(b - a for a, b in keys) / fps), lambda v: on_progress(0.95 + 0.05 * v))
            if code != 0:
                part.unlink(missing_ok=True)
                raise RuntimeError("FFmpeg failed:\n" + "\n".join(err.strip().splitlines()[-5:]))
            os.replace(part, out)
            on_progress(1.0)
        finally:
            if own_cache:
                cache.stop()


def next_output_path(src):
    src = Path(src)
    out = src.with_name(f"{src.stem}_edited.mp4")
    n = 2
    while out.exists():
        out = src.with_name(f"{src.stem}_edited_{n}.mp4")
        n += 1
    return out


# --- captions ------------------------------------------------------------------------------

WHISPER_MODEL = "small.en"   # downloaded once (about 480 MB) on first use
CAPTION_WORDS = {"low": (3.5, 4), "medium": (7, 8), "high": (10, 12)}  # (aim, most) words per caption
LINE_WORDS = 8               # longer captions wrap onto two lines
PAUSE = 0.2                  # a gap this long between words is a natural place to break
HOLD = 0.2                   # a caption stays up this long after its last word
FILL = 0.4                   # gaps shorter than this are bridged, so captions don't flicker
MIN_SHOW = 0.5               # shortest time a caption is on screen
# A caption never ends on one of these mid-sentence ("I went to the" / "store")
WEAK_ENDS = set("a an the to of and or but so for with in on at by from into about as than that this these those "
                "my your his her our their its i i'm i've i'd i'll we they he she like just very really super if "
                "because when one two three four five six seven eight nine ten".split())


def _clean(word):
    """The word as shown: no punctuation around it (apostrophes inside stay, as in "don't")."""
    return word.strip().strip('.,?!:;"…“”—')


def _break_cost(words, i):
    """Cost of ending a caption after words[i] when the sentence goes on."""
    text = _clean(words[i][2]).lower()
    if text in WEAK_ENDS or text.isdigit():
        return 100
    soft = words[i][2].strip()[-1:] in ",;:—" or (i + 1 < len(words) and words[i + 1][0] - words[i][1] >= PAUSE)
    return -3 if soft else 0


def _split(words, aim, most):
    """Cut one sentence into word groups of at most `most`, close to `aim` each, at the least awkward places."""
    n = len(words)
    best = [(0.0, 0)] + [(float("inf"), 0)] * n  # best[i] = (cost of words[:i], where its last group starts)
    for i in range(1, n + 1):
        for j in range(i - 1, max(0, i - most) - 1, -1):  # on a tie the earlier group stays longer
            c = best[j][0] + (i - j - aim) ** 2 + (_break_cost(words, i - 1) if i < n else 0)
            if c < best[i][0]:
                best[i] = (c, j)
    groups, i = [], n
    while i:
        groups.append(words[best[i][1]:i])
        i = best[i][1]
    return groups[::-1]


def _text(group):
    words = [_clean(w[2]) for w in group]
    if len(words) <= LINE_WORDS:
        return " ".join(words)
    k = min(range(1, len(words)), key=lambda k: (k - len(words) / 2) ** 2 + _break_cost(group, k - 1))
    return " ".join(words[:k]) + "\n" + " ".join(words[k:])


def make_captions(words, mode):
    """(start, end, text) captions from whisper's (start, end, word) list. Sentences never share a caption."""
    aim, most = CAPTION_WORDS[mode]
    words = [w for w in words if _clean(w[2])]
    groups, start = [], 0
    for i, w in enumerate(words):
        if w[2].strip()[-1:] in ".?!" or i == len(words) - 1:
            groups += _split(words[start:i + 1], aim, most)
            start = i + 1
    caps = []
    for k, g in enumerate(groups):
        begin, last = g[0][0], g[-1][1]
        nxt = groups[k + 1][0][0] if k + 1 < len(groups) else float("inf")
        end = nxt if nxt - last < FILL else min(max(last + HOLD, begin + MIN_SHOW), nxt)
        caps.append((begin, end, _text(g)))
    return caps


def to_srt(captions):
    def stamp(t):
        ms = round(t * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "".join(f"{n}\n{stamp(a)} --> {stamp(b)}\n{text}\n\n" for n, (a, b, text) in enumerate(captions, 1))


def transcribe(path, on_progress):
    """[(start, end, word)] for the English speech in `path`."""
    try:
        from faster_whisper import WhisperModel  # only needed when captions are on
    except ImportError:
        raise RuntimeError("Captions need faster-whisper. Install it with: pip install faster-whisper") from None
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    segments, info = model.transcribe(str(path), language="en", word_timestamps=True, vad_filter=True)
    words = []
    for seg in segments:
        words += [(w.start, w.end, w.word) for w in seg.words]
        on_progress(min(1.0, seg.end / info.duration) if info.duration else 0.0)
    return words


def write_captions(video, mode, on_progress):
    """Write `<video>.srt` next to the video and return its path."""
    caps = make_captions(transcribe(video, on_progress), mode)
    if not caps:
        raise RuntimeError("No speech was heard, so there are no captions.")
    srt = Path(video).with_suffix(".srt")
    srt.write_text(to_srt(caps), encoding="utf-8")
    on_progress(1.0)
    return srt


# --- review server -------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
RELOAD_GRACE = 5      # seconds a closed page has to come back (a reload) before we cancel
IDLE_TIMEOUT = 180    # no request at all for this long cancels (background tabs ping slowly)


class State:
    def __init__(self, src, info, db, pieces, tmp):
        self.src, self.info, self.db, self.pieces, self.tmp = Path(src), info, db, pieces, Path(tmp)
        self.phase, self.value, self.out, self.error, self.step = "review", 0.0, None, None, ""
        self.srt, self.caption_error = None, None
        self.last_seen, self.bye_at = time.time(), None
        self.lock = threading.Lock()
        self.cache = SegmentCache(src, info, self.tmp)  # also writes tonemap.cube there for HDR thumbnails
        self.cache.want(pieces, reviewed_until=0)

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

    def save(self, pieces, captions=None):
        self.phase, self.out = "saving", next_output_path(self.src)

        def work():
            try:
                render(self.src, self.info, pieces, self.out, lambda v: setattr(self, "value", v), self.cache,
                       lambda s: setattr(self, "step", s))
            except Exception as e:  # shown to the user on the page and in the console
                self.error, self.phase = str(e), "error"
                return
            if captions:
                self.phase, self.value, self.step = "captions", 0.0, "Making captions"
                try:
                    self.srt = write_captions(self.out, captions, lambda v: setattr(self, "value", v))
                except Exception as e:  # the video is already saved; only the captions are missing
                    self.caption_error = str(e)
            self.phase = "done"

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
                    self.send(200, json.dumps({"phase": state.phase, "value": state.value, "step": state.step,
                                               "ready": state.cache.progress(list(state.cache.wanted)),
                                               "out": str(state.out) if state.out else None, "error": state.error,
                                               "srt": str(state.srt) if state.srt else None,
                                               "caption_error": state.caption_error}))
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
            if self.path == "/pieces":  # the page's latest cuts, so the background render can keep up
                try:
                    msg = json.loads(body)
                    pieces = _valid_pieces(msg.get("pieces"), state.info["duration"])
                    reviewed = float(msg.get("reviewed_until", 0))
                except (ValueError, AttributeError, TypeError):
                    pieces = None
                if not pieces or state.phase != "review":
                    return self.send(400, "The cut list is not valid.", "text/plain")
                state.cache.want(pieces, reviewed)
                return self.send(204, b"")
            if self.path == "/save":
                if state.phase != "review":
                    return self.send(409, "Already saving.", "text/plain")
                try:
                    msg = json.loads(body)
                    pieces = _valid_pieces(msg.get("pieces"), state.info["duration"])
                    captions = msg.get("captions") or None
                except (ValueError, AttributeError):
                    pieces = None
                if not pieces or captions not in (None, *CAPTION_WORDS):
                    return self.send(400, "The cut list is not valid.", "text/plain")
                state.save(pieces, captions)
                return self.send(200, json.dumps({"out": str(state.out)}))
            self.send(404, "not found", "text/plain")

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def _bar(label, v):
    sys.stdout.write(f"\r  {label:<26} [{'#' * int(v * 30):-<30}] {v:4.0%} ")
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
    with tempfile.TemporaryDirectory(prefix="silence_cutter_", ignore_cleanup_errors=True) as tmp:
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
                    _bar(state.step or "Saving", state.value)
                elif state.phase == "captions":
                    _bar("Making captions", state.value)
                elif state.phase == "done":
                    _bar("Making captions" if state.srt or state.caption_error else "Saving", 1.0)
                    print(f"\n\n  Done! Saved to:\n  {state.out}\n")
                    if state.srt:
                        print(f"  Captions:\n  {state.srt}\n")
                    elif state.caption_error:
                        print(f"  The video is saved, but the captions failed:\n  {state.caption_error}\n")
                    time.sleep(1.5)  # let the page show "Saved"
                    break
                elif state.phase == "error":
                    raise SystemExit(f"\n  Saving failed:\n  {state.error}")
                elif state.cancelled():
                    print("  Cancelled. Nothing was saved.\n")
                    break
        except KeyboardInterrupt:
            print("\n  Cancelled. Nothing was saved.\n")
        finally:
            srv.shutdown()
            srv.server_close()
            state.cache.stop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            print(f"\n  {e.code}\n")
            sys.exit(1)
