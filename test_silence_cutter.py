"""Plain-assert checks. Run: python test_silence_cutter.py"""
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  ", name)
    print("all passed")
