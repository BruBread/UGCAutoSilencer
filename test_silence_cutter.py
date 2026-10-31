"""Plain-assert checks. Run: python test_silence_cutter.py"""
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


RAW = Path(r"C:\Users\user\Desktop\Jacobs Editing Shit\IMG_7006.MOV")
HAND = Path(r"D:\New folder\samplecut.mp4")


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
        print("     skipped test_sample_match (sample files not found)")
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  ", name)
    print("all passed")
