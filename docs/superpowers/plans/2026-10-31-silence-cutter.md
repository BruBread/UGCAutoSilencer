# Silence Cutter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `.bat`-launched tool that detects silences in a video, lets the user review the cuts in a browser page, and renders `<stem>_edited.mp4` next to the original.

**Architecture:** One Python file (`silence_cutter.py`) does the probing, the audio analysis, a local `http.server` for the review page, and the FFmpeg render. One static `review.html` holds the page. The `.bat` file only asks for the path.

**Tech Stack:** Python 3.11 stdlib + numpy, FFmpeg/ffprobe 8 on PATH, `h264_amf` with a `libx264` fallback, plain HTML/JS/canvas.

**Spec:** `docs/superpowers/specs/2026-10-31-silence-cutter-design.md`

## Global Constraints

- No new dependencies: stdlib + numpy only. No scipy.
- Windows only. Server binds `127.0.0.1` only.
- Output: H.264 `.mp4`, SDR BT.709, AAC 192 kb/s, constant frame rate at the source's `r_frame_rate`, upright.
- Never modify the original. Never overwrite: `<stem>_edited.mp4`, then `_edited_2`, `_3`, ...
- Starting constants: `THRESH_FRACTION = 0.65`, `MIN_GAP = 0.25`, `MIN_BLIP = 0.10`, `PAD = 0.04`, `FRAME = 0.02`, `FADE = 0.01`.
- Commit after each task with a plain one-sentence message and no Claude credit.

## Review Focus

- Paths with spaces, quotes, or non-ASCII characters (`C:\Users\user\Desktop\Jacobs Editing Shit\IMG_7006.MOV`): the tool must accept them. Covered in Task 4's manual run.
- A video with no audio stream: a clear message, no traceback. Test in Task 2.
- A video with no silence at all, or one that is entirely silent: the detection returns one piece, or no pieces with a message instead of a crash. Test in Task 1.
- The page is reloaded mid-review: no cancel. Covered in Task 4's manual check.
- The render fails halfway: no `_edited` file is left behind. Test in Task 3 (temp name, then rename).

---

### Task 1: Detection core and output naming

**Files:**
- Create: `silence_cutter.py`, `test_silence_cutter.py`

**Interfaces:**
- Produces: `loudness_db(samples: np.ndarray, sr: int) -> np.ndarray` (dB per `FRAME`), `detect_pieces(db: np.ndarray, duration: float) -> list[tuple[float, float]]` (kept pieces in seconds, sorted, non-overlapping, clamped to `[0, duration]`), and `next_output_path(src: Path) -> Path`.

- [ ] **Step 1: Write the failing tests** in `test_silence_cutter.py` (plain asserts, `if __name__ == "__main__"` runs all):
  - `test_detect_tones()`: 16 kHz signal, 6 s total. Steady noise at amplitude 0.003 throughout, plus 440 Hz tones at amplitude 0.3 over 0.5–1.5 s, 2.5–3.0 s, and 3.1–4.5 s. Expect 2 pieces (the 0.1 s gap is bridged), with `abs(start-0.5) <= PAD+FRAME` and `abs(end-4.5) <= PAD+FRAME` on the right boundaries.
  - `test_detect_all_silent()`: noise only gives `[]`.
  - `test_detect_no_silence()`: a tone the whole way gives one piece covering about `[0, duration]`.
  - `test_next_output_path()`: in a temp dir, `a.mov` gives `a_edited.mp4`. After creating that file, it gives `a_edited_2.mp4`.
- [ ] **Step 2: Run** `python test_silence_cutter.py`. Expected: ImportError / failures.
- [ ] **Step 3: Implement.** `loudness_db` gives the mean square per frame, in dB with a +1e-12 floor. In `detect_pieces`: noise floor = 10th percentile, speech level = 90th percentile. If `speech - floor < 6` dB, there is no contrast: return `[]` when the 90th percentile sits near the floor's absolute level (all quiet), otherwise `[(0, duration)]`. Otherwise threshold, bridge gaps `< MIN_GAP`, drop runs `< MIN_BLIP`, pad by `PAD`, merge overlaps, and clamp.
- [ ] **Step 4: Run tests.** Expected: all pass.
- [ ] **Step 5: Commit** "Add silence detection and output naming".

### Task 2: Probe, audio decode, and tuning on the sample

**Files:**
- Modify: `silence_cutter.py`, `test_silence_cutter.py`

**Interfaces:**
- Produces: `probe(path: Path) -> dict` with the keys `duration: float`, `fps: str` (for example `"30/1"`), `hdr: bool` (transfer is `arib-std-b67` or `smpte2084`), and `has_audio: bool`. Also `decode_audio(path: Path, sr: int = 16000) -> np.ndarray` (mono float32, through `highpass=f=100,lowpass=f=4000`) and `analyze(path: Path) -> tuple[dict, np.ndarray, list[tuple[float, float]]]`, which returns the probe info, the dB envelope, and the pieces. It raises `SystemExit` with a readable message when there is no audio.

- [ ] **Step 1: Tests.** `test_no_audio()` makes a 1 s `testsrc` clip with no audio in a temp dir via FFmpeg and asserts that `analyze` raises `SystemExit`. `test_sample_match()` is skipped (prints "skipped") unless both sample files from the spec exist. It aligns the hand edit to the raw file using the FFT chunk correlation from the brainstorm (0.2 s chunks, 8 kHz, normalized cross-correlation, runs of equal offset give the kept raw ranges). It prints the match %, the kept seconds, and the missing seconds, and asserts `missing < 1.0`.
- [ ] **Step 2: Run.** Expected: failures.
- [ ] **Step 3: Implement** `probe` (ffprobe JSON), `decode_audio`, and `analyze`.
- [ ] **Step 4: Run, then tune.** Adjust `THRESH_FRACTION`, `MIN_GAP`, and `PAD` until `missing < 1.0` with kept seconds as low as possible. Record the final numbers and match % in the commit message body.
- [ ] **Step 5: Commit** "Add probing and audio analysis tuned on the sample video".

### Task 3: Render

**Files:**
- Modify: `silence_cutter.py`, `test_silence_cutter.py`

**Interfaces:**
- Consumes: `probe`, `next_output_path`.
- Produces: `render(src: Path, info: dict, pieces: list[tuple[float, float]], out: Path, on_progress: Callable[[float], None]) -> None`. `on_progress` gets values from 0.0 to 1.0. It writes `out` atomically (`out.with_suffix(".part.mp4")`, then `os.replace`) and raises `RuntimeError` with FFmpeg's last stderr lines on failure.

- [ ] **Step 1: Tests.** `test_render_duration()`: build a 4 s `testsrc2` + `sine` clip, render pieces `[(0.5, 1.5), (2.0, 3.0)]`, and assert that the output duration from ffprobe is 2.0 ± 0.1 s and the progress callback reached ≥ 0.99. `test_render_failure_leaves_nothing()`: render from a non-existent source, assert `RuntimeError`, and assert that neither `out` nor the `.part` file exists.
- [ ] **Step 2: Run.** Expected: failures.
- [ ] **Step 3: Implement.** Write the filter graph to a temp file (`-filter_complex_script`): per piece `trim`/`setpts=PTS-STARTPTS` and `atrim`/`asetpts`, with `afade` in/out of `FADE` length, then `concat=n=N:v=1:a=1`. If `info["hdr"]`, append the SDR conversion to the video chain. First try `zscale=tin=arib-std-b67(or smpte2084):pin=bt2020:min=bt2020nc:t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p` on the real sample. If it errors, use `colorspace=all=bt709:iall=bt2020:itrc=bt2020-10:format=yuv420p`. Keep whichever works on `IMG_7006.MOV` and note it in a comment. Encoder args: `h264_amf -quality quality -rc cqp -qp_i 18 -qp_p 18`. If FFmpeg fails with an AMF/encoder error, retry once with `libx264 -preset veryfast -crf 18`. Also pass `-fps_mode cfr -r <fps> -c:a aac -b:a 192k -movflags +faststart -progress pipe:1 -nostats`. Parse `out_time_us`.
- [ ] **Step 4: Run tests,** then render the sample's detected pieces once and look at a frame (`ffmpeg -ss 5 -frames:v 1`) to check it's upright and the colors aren't washed out. Report the render time.
- [ ] **Step 5: Commit** "Add rendering with GPU encoding and HDR conversion".

### Task 4: Review page, server, launcher

**Files:**
- Create: `review.html`, `Silence Cutter.bat`
- Modify: `silence_cutter.py` (add `serve_review(...)` and `main()`)

**Interfaces:**
- Consumes: `analyze`, `render`, `next_output_path`.
- Server endpoints (all `127.0.0.1`, free port): `GET /` gives `review.html`. `GET /video` serves the source with Range support. `GET /preview` gives a 540p H.264 proxy, made on first request. `GET /data` gives JSON `{duration, frame: FRAME, db: [...], pieces: [[s, e], ...], name}`. `GET /frames?start=&end=&n=12` gives JSON with a list of JPEG URLs under `/thumb/<i>.jpg`, cached in the temp dir. `POST /save` takes body `{pieces}`, starts the render in a thread, and returns `{out}`. `GET /progress` gives `{phase: "review" | "saving" | "done" | "error", value, out, error}`. `POST /bye` comes from a pagehide beacon. `POST /ping` is the heartbeat.
- The page keeps a `pieces` array as its source of truth. A "silence" is the gap before each piece, between pieces, and after the last piece. Dragging the cut edges changes the neighboring piece ends. **S** (keep silence) merges the two neighbors. **D** deletes the piece under the slider.

- [ ] **Step 1: Build `review.html`** to the v3 mockup layout (`.superpowers/brainstorm/1386-1793403263/content/review-v3.html`): header, video with a red border when the playhead is inside a cut, a whole-video strip (click to jump), the zoomed window (current silence ± 1.5 s) with the frame strip over the waveform and draggable edges, a scrub slider, and the buttons and keys from the spec (Backspace, Space, S, D, Enter, Accept all remaining, Finish & save). Space plays from 1 s before the cut, jumps over it, and stops 1 s after. The video falls back to `/preview` on `error`. During saving, a progress bar polls `/progress` and shows the output path when done.
- [ ] **Step 2: Implement `main()`.** Read the path from `argv`. Print the step names with a console progress bar for analysis. Start the server, open the browser, and wait. Cancel when `/bye` arrives and no request follows within 5 s, or when there's no ping for 180 s; print "Cancelled", exit, and write nothing. On save, draw a console progress bar from the render callback (`\r[#####-----] 52%`), then print the output path. Delete the temp dir in a `finally`.
- [ ] **Step 3: `Silence Cutter.bat`:** `@echo off`, `cd /d "%~dp0"`, check that `python` and `ffmpeg` exist (with a friendly message if not), a `set /p` loop that strips quotes (`set "p=%p:"=%"`) and re-asks while the file doesn't exist, `python silence_cutter.py "%p%"`, `pause`.
- [ ] **Step 4: Manual end-to-end** on `IMG_7006.MOV` via the `.bat`: the page opens, the frames and waveform show, dragging and keys work, a reload doesn't cancel, closing the tab cancels, and Finish & save produces `IMG_7006_edited.mp4` next to the raw file. Report the total time. Run `python test_silence_cutter.py` again; all pass.
- [ ] **Step 5: Commit** "Add the review page, local server and launcher".
