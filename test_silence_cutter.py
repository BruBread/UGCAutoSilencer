"""Plain-assert checks. Run: python test_silence_cutter.py"""
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

import silence_cutter as sc

SR = 16000


def tone_signal(duration, tones, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(duration * SR)) / SR
    x = 0.003 * rng.standard_normal(len(t))
    for a, b in tones:
        m = (t >= a) & (t < b)
        x[m] += 0.3 * np.sin(2 * np.pi * 440 * t[m])
    return x.astype(np.float32)


def test_detect_tones():
    x = tone_signal(6.0, [(0.5, 1.5), (2.5, 3.0), (3.1, 4.5)])
    pieces = sc.detect_pieces(sc.loudness_db(x, SR), 6.0)
    tol = sc.PAD + sc.FRAME
    assert len(pieces) == 2, pieces
    assert abs(pieces[0][0] - 0.5) <= tol and abs(pieces[0][1] - 1.5) <= tol, pieces
    assert abs(pieces[1][0] - 2.5) <= tol and abs(pieces[1][1] - 4.5) <= tol, pieces


def test_detect_all_silent():
    x = tone_signal(3.0, [])
    assert sc.detect_pieces(sc.loudness_db(x, SR), 3.0) == []


def test_detect_no_silence():
    x = tone_signal(3.0, [(0, 3.0)])
    pieces = sc.detect_pieces(sc.loudness_db(x, SR), 3.0)
    assert len(pieces) == 1 and pieces[0][0] <= 0.05 and pieces[0][1] >= 2.95, pieces


def test_next_output_path():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.mov"
        first = sc.next_output_path(src)
        assert first.name == "a_edited.mp4", first
        first.touch()
        assert sc.next_output_path(src).name == "a_edited_2.mp4"


# Optional tuning check: a raw video and your own hand-cut version of it
RAW = Path(os.environ.get("SAMPLE_RAW", "missing-sample-raw"))
HAND = Path(os.environ.get("SAMPLE_EDIT", "missing-sample-edit"))


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def test_no_audio():
    with tempfile.TemporaryDirectory() as d:
        clip = Path(d) / "silent.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30", "-t", "1", str(clip))
        try:
            sc.analyze(clip)
        except SystemExit as e:
            assert "audio" in str(e).lower(), e
        else:
            raise AssertionError("expected SystemExit for a video without audio")


def hand_edit_ranges(raw, hand, sr=8000, win=0.2):
    """Raw-time ranges the hand edit kept, found by matching 0.2 s chunks of its audio."""
    w = int(win * sr)
    n = 1 << int(np.ceil(np.log2(len(raw) + w)))
    R = np.fft.rfft(raw, n)
    csum = np.concatenate([[0], np.cumsum(raw.astype(np.float64) ** 2)])
    runs = []  # [edit_start, edit_end, offset]
    for i in range(0, len(hand) - w, w):
        c = hand[i:i + w]
        e = np.sqrt((c ** 2).sum())
        if e < 1e-4:
            continue
        x = np.fft.irfft(R * np.conj(np.fft.rfft(c, n)), n)[: len(raw) - w]
        k = int(np.argmax(x / (np.sqrt(csum[w:w + len(x)] - csum[: len(x)]) + 1e-9)))
        off = k - i
        if runs and abs(runs[-1][2] - off) <= 2:
            runs[-1][1] = i + w
        else:
            runs.append([i, i + w, off])
    return [((a + o) / sr, (b + o) / sr) for a, b, o in runs]


def test_sample_match():
    if not (RAW.exists() and HAND.exists()):
        print("     skipped test_sample_match (set SAMPLE_RAW and SAMPLE_EDIT to run it)")
        return
    _, _, pieces = sc.analyze(RAW)
    raw, hand = sc.decode_audio(RAW, 8000, band=False), sc.decode_audio(HAND, 8000, band=False)
    step = 0.01
    t = np.arange(0, len(raw) / 8000, step)
    mine = np.zeros(len(t), bool)
    truth = np.zeros(len(t), bool)
    for a, b in pieces:
        mine[(t >= a) & (t < b)] = True
    for a, b in hand_edit_ranges(raw, hand):
        truth[(t >= a) & (t < b)] = True
    missing = (truth & ~mine).sum() * step
    print(f"     sample: kept {mine.sum() * step:.1f}s (hand edit {truth.sum() * step:.1f}s), "
          f"match {(mine & truth).sum() / (mine | truth).sum():.0%}, missing {missing:.2f}s, pieces {len(pieces)}")
    assert missing < 1.0, missing


def test_render_duration():
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "src.mp4", Path(d) / "out.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=f=300",
               "-t", "4", "-pix_fmt", "yuv420p", str(src))
        seen = []
        sc.render(src, sc.probe(src), [(0.5, 1.5), (2.0, 3.0)], out, seen.append)
        got = sc.probe(out)["duration"]
        assert abs(got - 2.0) <= 0.1, got
        assert sc.probe(out)["transfer"] == "bt709"
        assert seen and max(seen) >= 0.99, seen[-3:]


def stream_durations(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "csv=p=0",
                        str(path)], capture_output=True, text=True)
    return {kind: float(dur) for kind, dur in (line.split(",") for line in r.stdout.split())}


def test_cache_reuses_segments_and_keeps_sync():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=f=300",
               "-t", "4", "-pix_fmt", "yuv420p", str(src))
        info = sc.probe(src)
        cache = sc.SegmentCache(src, info, Path(d) / "cache")
        try:
            sc.render(src, info, [(0.5, 1.5), (2.0, 3.0)], Path(d) / "a.mp4", lambda p: None, cache)
            first = set((Path(d) / "cache").glob("seg_*.mp4"))
            assert len(first) == 2, first
            # moving one cut edge re-renders only the piece it touches
            sc.render(src, info, [(0.5, 1.5), (2.0, 3.2)], Path(d) / "b.mp4", lambda p: None, cache)
            assert len(set((Path(d) / "cache").glob("seg_*.mp4")) - first) == 1
        finally:
            cache.stop()
        durs = stream_durations(Path(d) / "b.mp4")
        assert abs(durs["video"] - 2.2) <= 0.04 and abs(durs["audio"] - durs["video"]) <= 0.04, durs


def test_render_failure_leaves_nothing():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out.mp4"
        info = {"duration": 4.0, "fps": "30/1", "hdr": False, "transfer": None, "has_audio": True}
        try:
            sc.render(Path(d) / "missing.mp4", info, [(0.0, 1.0)], out, lambda p: None)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected RuntimeError")
        assert list(Path(d).iterdir()) == [], list(Path(d).iterdir())


def test_server():
    import json
    import threading
    import urllib.error
    import urllib.request
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-f", "lavfi", "-i", "sine=f=300",
               "-t", "3", "-pix_fmt", "yuv420p", str(src))
        info, db, pieces = sc.analyze(src)
        state = sc.State(src, info, db, [(0.5, 1.0), (2.0, 2.5)], Path(d))
        srv = sc.make_server(state)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            data = json.load(urllib.request.urlopen(base + "/data"))
            assert data["pieces"] == [[0.5, 1.0], [2.0, 2.5]] and abs(data["duration"] - 3) < 0.1, data
            req = urllib.request.Request(base + "/video", headers={"Range": "bytes=10-19"})
            r = urllib.request.urlopen(req)
            assert r.status == 206 and len(r.read()) == 10
            assert r.headers["Content-Range"].startswith("bytes 10-19/")
            bad = urllib.request.Request(base + "/save", data=b'{"pieces": [[2, 1]]}', method="POST")
            try:
                urllib.request.urlopen(bad)
                raise AssertionError("bad pieces accepted")
            except urllib.error.HTTPError as e:
                assert e.code == 400
            ok = urllib.request.Request(base + "/pieces", method="POST",
                                        data=b'{"pieces": [[0.5, 1.0], [2.0, 2.6]], "reviewed_until": 1.5}')
            assert urllib.request.urlopen(ok).status == 204
            assert state.cache.wanted == [(15, 30), (60, 78)], state.cache.wanted
            ready = json.load(urllib.request.urlopen(base + "/progress"))["ready"]
            assert 0 <= ready <= 1, ready
            urls = json.load(urllib.request.urlopen(base + "/frames?start=0&end=2&n=4"))
            assert len(urls) == 4 and urllib.request.urlopen(base + urls[0]).read()[:2] == b"\xff\xd8"
        finally:
            srv.shutdown()
            srv.server_close()
            state.cache.stop()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  ", name)
    print("all passed")
