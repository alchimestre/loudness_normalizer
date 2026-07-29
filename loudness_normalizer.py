#!/usr/bin/env python3

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
import mutagen

FORMATS = {
    ".wav": {"codec": "pcm_s16le", "ar": "44100", "ext": "wav", "extra": []},
    ".aiff": {"codec": "pcm_s16be", "ar": "44100", "ext": "aiff", "extra": []},
    ".aif": {"codec": "pcm_s16be", "ar": "44100", "ext": "aiff", "extra": []},
    ".mp3": {"codec": "libmp3lame", "ar": "44100", "b": "320k", "ext": "mp3", "extra": []},
    ".m4a": {"codec": "aac", "ar": "44100", "b": "320k", "ext": "m4a", "extra": ["-movflags", "+faststart"]},
    ".opus": {"codec": "libopus", "ar": "48000", "b": "320k", "ext": "opus", "extra": []},
    ".ogg": {"codec": "libvorbis", "ar": "44100", "q": "9", "ext": "ogg", "extra": []},
    ".flac": {"codec": "flac", "ar": "44100", "ext": "flac", "extra": ["-sample_fmt", "s16"]},
}

MUTAGEN_EXTS = {".aiff", ".aif", ".mp3", ".m4a", ".flac"}
STATE_FILENAME = ".normalized_state.json"
TARGET_LUFS = "-14"
TRUE_PEAK = "-1.0"
NORMALIZED_THRESHOLD = 0.5
PRINT_LOCK = threading.Lock()

def clone_metadata(src_path: Path, dest_path: Path, suffix: str):
    try:
        if suffix == ".m4a":
            from mutagen.mp4 import MP4
            src_mp4, dest_mp4 = MP4(src_path), MP4(dest_path)
            if src_mp4.tags:
                dest_mp4.tags = src_mp4.tags
                dest_mp4.save()

        elif suffix == ".flac":
            from mutagen.flac import FLAC
            src_flac, dest_flac = FLAC(src_path), FLAC(dest_path)
            if src_flac.tags or src_flac.pictures:
                if dest_flac.tags is None:
                    dest_flac.add_tags()
                if src_flac.tags:
                    dest_flac.tags.update(src_flac.tags)
                for pic in src_flac.pictures:
                    dest_flac.add_picture(pic)
                dest_flac.save()

        else:
            src_audio = mutagen.File(src_path)
            if src_audio and src_audio.tags:
                dest_audio = mutagen.File(dest_path)
                dest_audio.tags = src_audio.tags
                if suffix in {".aiff", ".aif", ".mp3"}:
                    dest_audio.save(v2_version=3)
                else:
                    dest_audio.save()

    except Exception as e:
        with PRINT_LOCK:
            print(f"\nMetadata clone warning for {src_path.name}: {e}")

def normalize_with_ffmpeg(track: Path) -> tuple:
    suffix = track.suffix.lower()
    fmt = FORMATS.get(suffix)
    if not fmt:
        return "error", track, f"Unsupported format: {suffix}"

    analysis_cmd = [
        "ffmpeg", "-i", str(track),
        "-af", f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:print_format=json",
        "-f", "null", "-"
    ]

    try:
        proc = subprocess.run(analysis_cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return "error", track, f"Analysis failed:\n{proc.stderr}"

        match = re.search(r'\{[\s\S]*?"target_offset"[\s\S]*?\}', proc.stderr)
        stats = json.loads(match.group(0)) if match else None

        if not stats:
            return "error", track, "Could not parse loudnorm stats."

        if abs(float(stats.get("input_i", -100)) - float(TARGET_LUFS)) <= NORMALIZED_THRESHOLD:
            return "skipped", track, {"mtime": track.stat().st_mtime_ns, "size": track.stat().st_size}

        loudnorm_filter = (
            f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:"
            f"measured_I={stats['input_i']}:measured_LRA={stats['input_lra']}:"
            f"measured_TP={stats['input_tp']}:measured_thresh={stats['input_thresh']}:"
            f"offset={stats['target_offset']}:print_format=summary"
        )

        temp_file = track.with_name(f".tmp_{track.name}")

        codec_opts = ["-c:a", fmt["codec"]]
        if "b" in fmt: codec_opts.extend(["-b:a", fmt["b"]])
        if "q" in fmt: codec_opts.extend(["-q:a", fmt["q"]])
        if "ar" in fmt: codec_opts.extend(["-ar", fmt["ar"]])

        metadata_opts = ["-map_metadata", "-1"] if suffix in MUTAGEN_EXTS else ["-map_metadata", "0", "-map", "0"]

        encode_cmd = [
            "ffmpeg", "-i", str(track), "-af", loudnorm_filter,
            *codec_opts, *metadata_opts, *fmt.get("extra", []), "-y", str(temp_file)
        ]

        proc2 = subprocess.run(encode_cmd, capture_output=True, text=True, check=False)
        if proc2.returncode != 0:
            if temp_file.exists(): temp_file.unlink()
            return "error", track, f"Encoding failed:\n{proc2.stderr}"

        if not temp_file.exists():
            return "error", track, "Output file not created."

        if suffix in MUTAGEN_EXTS:
            clone_metadata(track, temp_file, suffix)

        temp_file.replace(track)
        return "normalized", track, {"mtime": track.stat().st_mtime_ns, "size": track.stat().st_size}

    except FileNotFoundError:
        return "error", track, "ffmpeg was not found in PATH."
    except Exception as e:
        return "error", track, f"Unexpected error: {e}"

def main():
    parser = argparse.ArgumentParser(description="Normalize an audio library to EBU R128 using ffmpeg.")
    parser.add_argument("directory", nargs="?", default=".", help="Music library root.")
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"{root} is not a directory.")
        return 1

    state_path = root / STATE_FILENAME
    state = load_state(state_path)

    for stale in root.rglob(".tmp_*"):
        if stale.is_file():
            try: stale.unlink()
            except OSError: pass

    tracks = []
    for file in root.rglob("*"):
        if not file.is_file() or file.name.startswith(".tmp_") or file.suffix.lower() not in FORMATS:
            continue

        rel = file.relative_to(root).as_posix()
        stat = file.stat()
        cached = state.get(rel, {})

        if cached.get("mtime") != stat.st_mtime_ns or cached.get("size") != stat.st_size:
            tracks.append(file)

    if not tracks:
        print("Library already normalized." if state else "No supported audio files found.")
        return 0

    workers = max(1, (os.cpu_count() or 1) - 1)
    print(f"Directory : {root}\nTracks    : {len(tracks)}\nWorkers   : {workers}\n")

    completed, normalized, skipped, errors = 0, 0, 0, 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(normalize_with_ffmpeg, t) for t in tracks]
        for future in concurrent.futures.as_completed(futures):
            completed += 1
            status, path, info = future.result()

            if status in ("skipped", "normalized"):
                state[path.relative_to(root).as_posix()] = info
                if status == "skipped": skipped += 1
                else: normalized += 1
            else:
                errors += 1
                with PRINT_LOCK:
                    print(f"\nERROR: {path}\n{info}")

            draw_progress(completed, len(tracks), path.name)

    print("")

    valid = {
        file.relative_to(root).as_posix(): state[file.relative_to(root).as_posix()]
        for file in root.rglob("*")
        if file.is_file() and file.suffix.lower() in FORMATS and file.relative_to(root).as_posix() in state
    }
    save_state(state_path, valid)

    print(f"Finished. {normalized} normalized, {skipped} already at target (skipped), {errors} error(s).")
    return 0

def draw_progress(done, total, filename):
    with PRINT_LOCK:
        width = 30
        filled = done * width // total
        bar = "=" * filled + "-" * (width - filled)
        sys.stdout.write(f"\r[{done}/{total}] [{bar}] {filename[:40]:40}\033[K")
        sys.stdout.flush()

def load_state(path: Path):
    if not path.exists(): return {}
    try:
        with path.open("r", encoding="utf-8") as f: return json.load(f)
    except Exception:
        print("Warning: state file is corrupted. Rebuilding.")
        return {}

def save_state(path: Path, state):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f: json.dump(state, f)
    tmp.replace(path)

if __name__ == "__main__":
    raise SystemExit(main())
