# UGC Auto Silencer

Cut the dead air out of talking-head videos in a couple of minutes instead of by hand.

You record a video, say your lines, and look away between them. UGC Auto Silencer finds every pause, shows each cut in your browser so you can check and adjust it, and saves a tightly cut copy next to the original. You can then drop that copy into CapCut (or any editor) and keep going.

## Features

- **Finds the silences for you.** It measures each video's own noise floor and speech level, so fans, AC hum and room noise don't count as talking.
- **Review every cut before anything is saved.** A local browser page shows the video, a frame strip and the audio waveform for each silence, so you can see exactly where you look away.
- **Drag to adjust.** Move the red cut edges, scrub with the slider, and play across the cut to check it sounds natural.
- **Remove retakes.** Put the slider on a flubbed take and press **D** to delete it.
- **Captions.** Optionally writes an `.srt` caption file next to the video, in short, medium or full-sentence lines.
- **Fast saves.** Kept pieces are rendered in the background while you review, so saving is often instant.
- **Works with iPhone footage.** It handles HEVC, HDR (Dolby Vision / HLG), rotated and variable frame rate video, and converts HDR to normal colors that match a CapCut export.
- **Safe.** The original is never touched. Output goes to `<name>_edited.mp4`, then `_edited_2`, and so on, and is never overwritten.

## Requirements

- Windows 10 or 11
- [Python](https://www.python.org/downloads/) 3.10 or newer, with `numpy` installed (and `faster-whisper` for captions)
- [FFmpeg](https://ffmpeg.org/download.html) on your `PATH` (a full build, for the `zscale` filter used for HDR)
- Optional: an AMD graphics card for faster encoding with `h264_amf`. Without one, it falls back to `libx264` on the CPU.

The quickest way to install the requirements:

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
pip install -r requirements.txt
```

## Usage

1. Double-click **`Silence Cutter.bat`**.
2. Drag your video into the window (or paste its path) and press Enter. You can also drop a video straight onto the `.bat` file.
3. Your browser opens the review page. Go through the cuts and adjust any that need it.
4. Press **Save as is** (or **Ctrl+S**). The finished `<name>_edited.mp4` appears next to the original, and the window closes.

Closing the browser tab without saving cancels, and nothing is written.

### Captions

Pick a mode in the **Captions** menu next to the save button before you save:

| Mode | Words per caption |
|---|---|
| **Off** | No captions (the default) |
| **Low** | 3 to 4, TikTok style |
| **Medium** | 6 to 8 |
| **High** | Full sentences, up to 12 words, wrapped onto two lines |

After the video is saved, its speech is transcribed and `<name>_edited.srt` is written next to it, so CapCut and most players pick it up. Captions have no punctuation. They never run from one sentence into the next, break at commas and pauses where possible, and never end on a word like "the", "to" or "my".

Transcription runs on your computer with Whisper (`small.en`, English only). The first time, it downloads the speech model (about 480 MB), so it needs internet once. After that it works offline and takes roughly as long as the video. If captions fail, the video is still saved.

### Review page controls

| Key | Action |
|---|---|
| **Enter** | Accept this cut and go to the next one |
| **Backspace** | Go back to the previous cut |
| **Space** | Play across the cut (about 1 s before and after, skipping the cut) |
| **S** | Keep this silence (don't cut it) |
| **D** | Delete the spoken piece under the slider (for retakes) |
| **[** / **]** | Set the cut start / end at the slider |
| **←** / **→** | Step one frame |
| **Ctrl+S** | Save everything as it is now |

You can also drag the red edges, scrub with the slider, and click any red mark on the whole-video strip to jump to it. The label next to the save button tells you when saving will be instant.

## How it works

1. **Detection.** FFmpeg extracts the audio through a voice-range filter (100 Hz to 4 kHz). The loudness is measured in 20 ms frames. The silence threshold sits 70% of the way from the video's noise floor (10th percentile) to its speech level (90th percentile). Gaps shorter than 0.25 s stay in, and 0.08 s is kept around each spoken piece.
2. **Review.** A small server on `127.0.0.1` serves the review page, the video (with seeking), and frame thumbnails. Nothing leaves your computer.
3. **Rendering.** Each kept piece is rendered to its own file in the background, at below-normal CPU priority. When you change a cut, only the pieces next to it are redone. Pieces are snapped to whole frames, so the audio stays in sync.
4. **Saving.** The pieces are joined without re-encoding. The audio is cut from the original with 10 ms fades at each cut, to avoid clicks. The output is H.264 `.mp4`, SDR BT.709, AAC 192 kb/s, at a constant frame rate.

HDR footage is converted with a 3D lookup table built from FFmpeg's `zscale` + `tonemap=hable` chain. It looks the same as running that chain on every frame, at about a quarter of the CPU cost.

## Tuning

The detection settings are constants at the top of [`silence_cutter.py`](silence_cutter.py):

| Constant | Default | Meaning |
|---|---|---|
| `THRESH_FRACTION` | `0.70` | Silence line between noise floor (0) and speech level (1). Higher cuts more. |
| `MIN_GAP` | `0.25` | Pauses shorter than this (seconds) are kept. |
| `PAD` | `0.08` | Seconds kept before and after each spoken piece. Lower is tighter. |
| `MIN_BLIP` | `0.10` | Sounds shorter than this are ignored. |
| `FADE` | `0.01` | Audio fade at each cut, in seconds. |
| `CAPTION_WORDS` | see code | Target and maximum words per caption for each mode. |
| `WEAK_ENDS` | see code | Words a caption may not end on mid-sentence. |

The defaults were tuned against a hand-edited iPhone video. Those settings keep everything the editor kept, apart from 0.74 s.

## Tests

```powershell
python test_silence_cutter.py
```

The tests use FFmpeg-generated clips, so they need no sample files. To also check detection against your own editing style, point two environment variables at a raw video and your hand-cut version of it:

```powershell
$env:SAMPLE_RAW = "C:\path\to\raw.MOV"
$env:SAMPLE_EDIT = "C:\path\to\your_hand_edit.mp4"
python test_silence_cutter.py
```

## Limitations

- Windows only, because of the `.bat` launcher and Windows process priorities.
- Retakes aren't detected automatically; remove them with **D** on the review page.
- Output is always SDR H.264. HDR output isn't supported.
- Captions are English only and are written as a separate `.srt` file, not burned into the video.
- Videos with hundreds of cuts haven't been tested.
