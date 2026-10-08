# DJ Library LUFS Normalizer

This tool makes all your audio files the same loudness. It uses EBU R128, the
loudness standard. It works with the formats that Pioneer CDJ and Rekordbox use.

## What it does

- Makes every track the same loudness (-14 LUFS by default).
- Works on many tracks at once. It uses one worker per CPU core, minus one.
- Works only on tracks that changed. It saves each track's fingerprint in
  `.normalized_state.json`.
- Keeps your tracks' dynamics. It never compresses audio. If the gain would
  break the true peak ceiling, the tool leaves the track alone and tells you.
  It also leaves tracks alone when FFmpeg cannot use its linear mode, including
  unset measurements or a measured loudness range above its 50 LU maximum.
- Keeps your metadata. Tags and cover art stay intact.
- Writes to a temp file first. It replaces the original only when the new file
  is good.

## Terms

- **LUFS** — loudness. Lower is quieter. -14 LUFS is the level streaming services use.
- **dBTP** — true peak. The loudest point of the sound, measured precisely.
- **LRA** — loudness range. The difference between the quiet and loud parts of
  a track.

## Install

1. Install FFmpeg:

```bash
brew install ffmpeg
```

2. Install the Python packages:

```bash
pip install -r requirements.txt
```

## Use

```bash
python loudness_normalizer.py [directory] [options]
```

If you do not give a directory, it uses the current one.

Examples:

```bash
# Normalize a folder (for example, an external USB drive or a playlist)
python loudness_normalizer.py "/Volumes/DJ_Drive/Techno_Set"

# Normalize to a different loudness and true peak
python loudness_normalizer.py --target-lufs -12 --true-peak -2.0 "/Volumes/DJ_Drive/Techno_Set"
```

## Options

| Option | Default | What it does |
|--------|---------|--------------|
| `--target-lufs` | `-14` | Target loudness in LUFS. Range: -70 to -5. |
| `--true-peak` | `-1.0` | Highest true peak in dBTP. Range: -9 to 0. |
| `--tolerance` | `0.5` | How close a track must be to the target to count as done, in LUFS. |

## How it decides what to process

A track needs work when:

- it has no state entry (new track), or
- its size or modification time changed, or
- the settings changed.

The settings are part of the fingerprint. Change a setting, and the tool
re-checks every track.

## Formats

- Lossless (WAV, AIFF, FLAC): kept at 16-bit / 44.1 kHz.
- Lossy (MP3, M4A): re-encoded at 320 kbps (MP3) or 256 kbps (AAC).
