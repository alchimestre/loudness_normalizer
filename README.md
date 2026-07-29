# DJ Library LUFS Normalizer

A high-performance, idempotent batch normalizer for DJ music libraries. It recursively scans directories, normalizes audio to the EBU R128 standard (-14 LUFS / -1.0 dB True Peak), and leverages multiprocessing to saturate multi-core CPUs (like Apple Silicon).

Designed specifically for Pioneer CDJ/Rekordbox compatibility, it includes custom metadata handling to ensure track information remains fully intact across all formats.

## Features
- Multiprocessing Engine: Spawns concurrent worker threads (automatically scaled to your CPU core count) to bypass FFmpeg's single-threaded limitations.
- Idempotent Execution: Tracks file modification times (mtime) and file sizes in a hidden .normalized_state.json file. Subsequent runs only process brand new or explicitly modified tracks.
- Pioneer CDJ Safe: Uses Mutagen to perfectly clone entire container-native metadata structures (ID3 chunks, Vorbis comments, and MP4 atoms) for AIFF, FLAC, M4A, and MP3 files. It strictly enforces ID3v2.3 encoding to prevent Rekordbox tag concatenation bugs.
- Atomic Overwrites: Writes to hidden temporary files during the normalization math and only overwrites the master file upon a 0 exit code.
- Broad Format Support: Natively handles .wav, .aiff, .aif, .mp3, .m4a, .flac, .opus, and .ogg.

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
Run the script from your terminal. It accepts a single, optional argument for the target directory. If no directory is provided, it defaults to the current working directory.

Target a specific directory (e.g., an external USB or specific playlist folder):

```bash
python loudness_normalizer.py "/Volumes/DJ_Drive/Techno_Set"
```

Run in the current directory:

```bash
python loudness_normalizer.py
```

- Lossless Formats (WAV/AIFF/FLAC): Audio is processed and maintained at 16-bit / 44.1kHz.
- Lossy Formats (MP3/M4A/OPUS/OGG): Audio is re-encoded at 320kbps (or VBR equivalent) to minimize generation loss. Opus is strictly targeted to its native 48kHz sample rate.
- Limiter: The True Peak limiter acts as a ceiling. Because modern club tracks are often mastered significantly louder than -14 LUFS, the script primarily turns tracks down, meaning the -1.0 dBTP limiter rarely engages, fully preserving the original dynamic range.
