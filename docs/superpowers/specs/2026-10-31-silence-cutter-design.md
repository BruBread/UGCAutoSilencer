# Silence Cutter: design

## Intent

The user records short talking-head videos (usually under 3 minutes) on an iPhone and edits them in CapCut. Most of their editing time goes to cutting dead air: after each line they look away from the camera, and that moment looks awkward if it stays in. This tool does that trim for them, with a quick human review of every cut, and hands back a file they drag into CapCut.

Success means: double-click, paste a path, review the cuts in a browser, get `<name>_edited.mp4` next to the original, fast. The cuts should land as tight as the user's own hand edits.

## Goals

- Detect silences in any common video (`.mov`, `.mp4`, `.mkv`, ...; HEVC or H.264; HDR or SDR; any resolution, rotation, variable frame rate).
- Ignore steady background noise (fans, AC, static, hum) when deciding what is silence.
- Let the user review and adjust every cut in a browser page before anything is rendered.
- Render a high-quality H.264 `.mp4` quickly using the AMD GPU.
- Show a progress bar in the console window (and in the browser while saving).

## Non-goals

- Noise reduction on the output audio (the user does that in CapCut).
- Automatic retake / flub detection (the user removes bad takes by hand on the review page).
- Filler-word ("um") removal, batch processing, uploading anywhere, HDR output, editing CapCut projects.
- Supporting operating systems other than Windows.

## Anti-goals (would make it a failure even if it works)

- Loose cuts that leave the look-away in. Tight beats loose.
- Clipped words.
- Touching or overwriting the original video, or overwriting an earlier `_edited` file.
- Audible clicks at cut points.
- Slow enough that the user would rather cut by hand.

## Environment (verified on the user's PC)

- Windows 11, FFmpeg 8.0.1 full build (on PATH, includes `h264_amf`, `zscale`, `tonemap`, `libplacebo`), Python 3.11.9 with numpy 2.4.6. No scipy. Nothing new may be installed.
- GPU: AMD Radeon RX 640. `h264_amf` encode works. It cannot encode HDR (10-bit).

## Reference sample (ground truth for tuning)

- Raw: `C:\Users\user\Desktop\Jacobs Editing Shit\IMG_7006.MOV`. 98.1 s, HEVC Main 10, 1080x1920 after rotation (display matrix -90), HLG / Dolby Vision, nominal 30 fps but variable, AAC 48 kHz stereo.
- User's hand edit: `D:\New folder\samplecut.mp4`. 39.2 s, 16 kept pieces, each 1.2 to 3.8 s long.
- Audio alignment of the two files showed: the user leaves 0 to 0.06 s before the first word of a piece and 0 to 0.14 s after the last word. About 8 s of what they removed is talking-volume audio (retakes) at roughly 0:13-0:17, 0:30-0:31, 0:45-0:47, 1:08-1:10, 1:14-1:16 of the raw file.
- A simulated silence cutter (20 ms frames, threshold at 65% of the way from noise floor to speech level, gaps of 0.2-0.3 s or more cut, no padding) matched 74% of the hand edit, kept 47.6 s, and missed 2.4 s of kept material. The extra ~8 s is the retakes. Padding of about 0.04 s should cut the missed material. The tuning target: keep everything the user kept (missing under ~1 s) while getting as close to 39 s as silence alone allows.

## User flow (decided with the user)

1. Double-click `Silence Cutter.bat`. It asks for the video path. Quotes from "Copy as path" are accepted. A path that doesn't exist re-asks.
2. The console shows a progress bar while the tool reads the file, analyzes the audio, and prepares the review page. It then opens the review page in the default browser automatically.
3. Review page (layout as in the approved v3 mockup):
   - Header: "Silence N of M" and old length → new length.
   - Video preview showing the frame under the slider. When the slider is inside a cut, the preview gets a red border and a "THIS PART GETS CUT" label.
   - "Whole video" strip: audio waveform of the full video with every cut in red. Clicking a cut jumps to it.
   - Zoomed view of the current silence (a few seconds around it) with two rows: a strip of video frames on top and the audio waveform below. One red cut region spans both rows. Its two edges ("Cut starts" / "Cut ends") are draggable.
   - A slider under the zoomed view scrubs the playhead.
   - Buttons and keys: Previous (Backspace), Play across cut (Space: plays about 1 s before the cut, skips the cut, plays about 1 s after), Keep this silence (S), Accept & next (Enter), Delete this take (D: removes the whole spoken piece the slider is in, for retakes), Accept all remaining, Finish & save (always visible).
4. Saving: the console and the page both show a progress bar while the GPU renders. Output goes to the original's folder as `<stem>_edited.mp4`. If that exists, `<stem>_edited_2.mp4`, `_3`, and so on.
5. The console says where the file is and waits for a key press before closing.

Closing the browser without saving means nothing is written, and the console says it was cancelled. The original is never modified. Temporary files are deleted at the end.

## How it works

### Files

- `Silence Cutter.bat`: asks for the path, strips quotes, runs `python silence_cutter.py "<path>"`, then `pause`.
- `silence_cutter.py`: everything else (probe, analysis, local web server, render). Stdlib plus numpy.
- `review.html`: the review page (inline CSS/JS, no external libraries, works offline).
- `test_silence_cutter.py`: the check (see Testing).

### Probe

`ffprobe` JSON gives duration, video codec, nominal frame rate (`r_frame_rate`), `color_transfer`, rotation, and whether there is an audio stream. No audio stream → clear error message, exit.

### Silence detection

1. Decode audio with FFmpeg to mono 16 kHz float32 through a voice band-pass (`highpass=f=100,lowpass=f=4000`). This removes AC rumble and much fan hum.
2. Loudness per 20 ms frame in dB.
3. Noise floor = 10th percentile, speech level = 90th percentile, both per video. Threshold = floor + fraction × (speech − floor). The starting fraction is 0.65, to be tuned on the sample.
4. Frames above the threshold are speech. Bridge speech gaps shorter than the minimum cut gap (start at 0.25 s). Drop speech blips shorter than 0.1 s.
5. Pad each speech piece by about 0.04 s on both sides, clamped so pieces don't overlap.
6. Result: a list of kept pieces `(start, end)` in seconds. The silences between them are what the review page walks through, including any leading or trailing silence.

All tuning numbers live as named constants at the top of `silence_cutter.py`.

### Local server and review page

- `http.server` on `127.0.0.1` with a free port, opened with `webbrowser.open`.
- Endpoints (the builder may rename them): the page, the video file (with HTTP Range support so seeking works), the analysis JSON (duration, loudness envelope, kept pieces), frame thumbnails for a time range, progress, and save.
- Preview video: the page first tries to play the original file directly. Chrome and Edge on this PC can usually play iPhone HEVC with GPU decoding. If the `<video>` element errors, the server makes a small H.264 preview copy (about 540p) and the page switches to it. Measured: about 3 s per 15 s of 1080p HEVC.
- Frame strip: generated on demand per silence with FFmpeg (fast seek, about 12 small JPEG frames across the zoomed window) and cached in the temp folder.
- Waveform: drawn on a canvas from the loudness envelope.
- Cancel detection: when the page closes, it sends `navigator.sendBeacon` on `pagehide`. The server waits about 5 s for the page to reconnect (so a page reload doesn't cancel), then exits, writes nothing, and prints "Cancelled". There is also a backup heartbeat with a long timeout (about 3 min), because browsers slow down timers in background tabs to about once a minute.

### Render

- One FFmpeg run: per kept piece, `trim`/`atrim` + `setpts`/`asetpts`, then `concat`. Each audio piece gets a short fade in and out (about 10 ms) to prevent clicks.
- Constant frame rate output at the source's nominal rate (`-fps_mode cfr -r <r_frame_rate>`), so CapCut gets a clean file from variable frame rate iPhone footage.
- Video: `h264_amf` at high quality (constant QP around 18; builder tunes for visually lossless, larger files are fine). If AMF fails to open, fall back to `libx264 -preset veryfast -crf 18` so it still works.
- Audio: AAC 192 kb/s, source sample rate.
- Rotation: FFmpeg's default autorotate applies, so the output is upright with no rotation flag.
- HDR (`color_transfer` is `arib-std-b67` or `smpte2084`): convert to SDR BT.709 before encoding. Preferred: a proper tone-mapping chain (`zscale` + `tonemap`, or `libplacebo`). In testing on synthetic clips both failed with "no path between colorspaces" / Vulkan init errors. The builder must test on the real sample file first. If a proper chain can't be made to work, fall back to the `colorspace` filter, which works but may look slightly flat. SDR input skips this step.
- Progress: `-progress pipe:1`. `out_time_us` divided by the output's total duration drives a text progress bar in the console (`\r` updates) and the page's progress bar.
- Output name: `<stem>_edited.mp4`, numbered if taken. Write to a temp name in the same folder and rename at the end, so a failed render never leaves a half-written `_edited` file.

### Speed (measured on fake clips, 3-minute equivalents)

- Audio analysis: about 2 s.
- Preview copy, only if needed: about 40 s at 1080p, about 80 s at 4K.
- Final render: about 1 min for 1080p, about 3.5 min for 4K HDR (HEVC decoding on the CPU dominates).

## Testing

`test_silence_cutter.py`, plain asserts, runnable with `python test_silence_cutter.py`:

- Detection on a synthetic signal (tones with silent gaps plus steady noise): the pieces found match the tone positions within padding.
- If the two sample files exist, run detection on the raw file and compare it with the hand edit (aligned the same way as in the brainstorm: FFT cross-correlation of 0.2 s chunks). Assert that the missing time is under about 1 s and print the match %.
- Output naming: `_edited`, then `_edited_2`.

Manual check before handing over: run the full flow on `IMG_7006.MOV`. The output plays upright, colors look right, there are no clicks, it imports into CapCut, and the timing is reported.

## Calls made by Claude (not explicitly decided by the user)

- Python + FFmpeg behind a `.bat`, review page in the browser served locally.
- Always re-encode (no stream copy), because stream-copy cuts snap to keyframes and would leave look-aways in.
- Output always H.264 `.mp4`, SDR, high quality.
- Tuning numbers (0.65 threshold fraction, 0.25 s minimum gap, 0.04 s padding, 10 ms fades), to be tuned against the sample.
- "Delete this take", "Accept all remaining", and "Finish & save" on the review page.
- Numbered output instead of overwriting. Heartbeat-based cancel. Temp files deleted at the end.
- libx264 fallback when AMF fails.

## Left to the builder

Endpoint names, exact page styling within the v3 layout, the arrow-key nudge step, the zoom window size, preview resolution, thumbnail count, and exact encoder flags.
