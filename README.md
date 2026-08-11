# DJ Library LUFS Normalizer

A high-performance, idempotent batch normalizer for DJ music libraries. It recursively scans directories, normalizes audio to the EBU R128 standard (-11 LUFS / -1.0 dB True Peak by default), and processes tracks in parallel to saturate multi-core CPUs (like Apple Silicon).

Designed specifically for Pioneer CDJ/Rekordbox compatibility, it includes custom metadata handling to ensure track information remains fully intact across all formats.

## Features
- Parallel Engine: Spawns concurrent workers (one per CPU core, minus one), each driving a single-threaded FFmpeg subprocess, so all cores stay saturated without oversubscription.
- Idempotent Execution: Each track is fingerprinted by mtime, size, and the four normalization settings in a hidden .normalized_state.json file. Subsequent runs only process brand new, modified, or settings-changed tracks. Old state entries (no settings) are treated as unprocessed, so a settings migration re-encodes nothing until an actual run.
- Settings-Aware: The target loudness, true peak, LRA, and tolerance are part of the idempotency fingerprint. Change any of them (via CLI options) and tracks re-encode — provided the re-encode would not destroy their dynamic range (see Dynamic Range Protection).
- Dynamic Range Protection: Normalization only ever runs in ffmpeg loudnorm's *linear* mode (constant gain, dynamics fully preserved). If the requested settings would force the *dynamic* mode fallback (compression), the track is left untouched and flagged with a warning instead of being compressed. A post-encode assertion confirms loudnorm ran linearly before the original file is overwritten.
- Pioneer CDJ Safe: Uses Mutagen to perfectly clone entire container-native metadata structures (ID3 chunks, Vorbis comments, and MP4 atoms) for AIFF, FLAC, M4A, and MP3 files. It strictly enforces ID3v2.3 encoding to prevent Rekordbox tag concatenation bugs.
- Atomic Overwrites: Writes to hidden temporary files during the normalization math and only overwrites the master file upon a 0 exit code.
- Broad Format Support: Natively handles .wav, .aiff, .aif, .mp3, .m4a, and .flac.
- Output Verification: After processing, the output's integrated loudness is checked against the target; tracks that cannot reach it (sparse, peak-limited material) are flagged as off-target warnings instead of silently passing.

## Requirements
The script relies on native Python 3 and the FFmpeg ecosystem.

1. Install FFmpeg:

```bash
brew install ffmpeg
```

2. Install Python dependencies:

```bash
pip install -r requirements.txt
```

## Usage
Run the script from your terminal. It accepts a single, optional argument for the target directory. If no directory is provided, it defaults to the current working directory. The normalization settings are CLI options with sane DJ defaults.

```bash
python loudness_normalizer.py [directory] [--target-lufs LUFS] [--true-peak DBTP] [--lra LU] [--tolerance LUFS]
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--target-lufs` | `-11` | Integrated loudness target in LUFS (EBU R128). Range [-70, -5]. |
| `--true-peak` | `-1.0` | Maximum true peak in dBTP. Range [-9, 0]. |
| `--lra` | `7` | Target loudness range in LU. Tracks with wider dynamics than this cannot be normalized without compression and are skipped. Range [1, 50]. |
| `--tolerance` | `0.5` | LUFS tolerance for "already at target". |

Examples:

```bash
# Target a specific directory (e.g., an external USB or specific playlist folder)
python loudness_normalizer.py "/Volumes/DJ_Drive/Techno_Set"

# Normalize to a different loudness / true peak
python loudness_normalizer.py --target-lufs -14 --true-peak -2.0 "/Volumes/DJ_Drive/Techno_Set"

# Run in the current directory
python loudness_normalizer.py
```

- Lossless Formats (WAV/AIFF/FLAC): Audio is processed and maintained at 16-bit / 44.1kHz.
- Lossy Formats (MP3/M4A): Audio is re-encoded at 320kbps (MP3) / 256kbps (AAC) to minimize generation loss.
- Dynamic Range: Normalization is always linear gain — the -11 LUFS target is reached by turning tracks down, which preserves the original dynamic range exactly. Turning tracks up is only attempted when the gain would not push the true peak past the ceiling; otherwise the track is left untouched and flagged (never compressed).
