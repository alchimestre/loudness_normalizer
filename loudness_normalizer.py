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
    ".wav": {"codec": "pcm_s16le", "ar": "44100", "extra": []},
    ".aiff": {"codec": "pcm_s16be", "ar": "44100", "extra": []},
    ".aif": {"codec": "pcm_s16be", "ar": "44100", "extra": []},
    ".mp3": {"codec": "libmp3lame", "ar": "44100", "b": "320k", "extra": []},
    ".m4a": {"codec": "aac", "ar": "44100", "b": "256k", "extra": ["-movflags", "+faststart"]},
    ".flac": {"codec": "flac", "ar": "44100", "extra": ["-sample_fmt", "s16"]},
}

MUTAGEN_EXTS = {".aiff", ".aif", ".mp3", ".m4a", ".flac"}
STATE_FILENAME = ".normalized_state.json"

# Defaults. Every setting is a CLI option and part of the state fingerprint.
DEFAULT_TARGET_LUFS = -14.0
DEFAULT_TRUE_PEAK = -1.0
DEFAULT_TOLERANCE = 0.5

PRINT_LOCK = threading.Lock()


class Params:
    """Normalization settings. as_dict() feeds the idempotency fingerprint."""

    __slots__ = ("target_lufs", "true_peak", "tolerance")

    def __init__(self, target_lufs, true_peak, tolerance):
        self.target_lufs = target_lufs
        self.true_peak = true_peak
        self.tolerance = tolerance

    def as_dict(self):
        return {
            "target_lufs": self.target_lufs,
            "true_peak": self.true_peak,
            "tolerance": self.tolerance,
        }


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


def needs_processing(cached, stat, params) -> bool:
    """A track needs processing iff its fingerprint changed: no state entry,
    a different mtime/size, or different settings. Entries without settings
    keys (legacy) therefore count as changed."""
    if not cached:
        return True
    if cached.get("mtime") != stat.st_mtime_ns or cached.get("size") != stat.st_size:
        return True
    return any(cached.get(key) != value for key, value in params.as_dict().items())


def linear_feasible(stats, params) -> tuple:
    """Check whether loudnorm can apply the required gain without compression.

    Gain must keep the true peak at or under the ceiling. Loudness range is
    limited only by loudnorm's maximum setting of 50 LU, not a target range.

    The last check mirrors loudnorm's init(): when it trips, loudnorm treats
    the measurements as unset, ignores them and runs in dynamic mode, which
    compresses. The comparisons use the values as printed by the analysis
    pass, because those exact strings are what the encoder receives.

    Returns (ok, reason); reason is "" when ok."""
    offset = params.target_lufs - float(stats["input_i"])
    offset_tp = float(stats["input_tp"]) + offset
    if float(stats["input_lra"]) > 50.0:
        return False, ("loudness range exceeds loudnorm's maximum of 50 LU; "
                       "it cannot normalize this track linearly")
    if offset_tp > params.true_peak:
        return False, (f"reaching {params.target_lufs} LUFS needs {offset:+.1f} dB, "
                       f"which would put the true peak at {offset_tp:+.1f} dBTP "
                       f"(ceiling {params.true_peak:+.1f} dBTP)")
    if (float(stats["input_lra"]) == 0.0 or float(stats["input_i"]) == 0.0
            or float(stats["input_tp"]) == 99.0 or float(stats["input_thresh"]) == -70.0):
        return False, "loudnorm would ignore the measurements and run in dynamic mode"
    return True, ""


def normalize_with_ffmpeg(track: Path, params: Params) -> tuple:
    suffix = track.suffix.lower()
    fmt = FORMATS.get(suffix)
    if not fmt:
        return "error", track, f"Unsupported format: {suffix}"

    analysis_cmd = [
        "ffmpeg", "-threads", "1", "-filter_threads", "1",
        "-i", str(track),
        "-af", f"loudnorm=I={params.target_lufs}:TP={params.true_peak}:print_format=json",
        "-f", "null", "-"
    ]

    try:
        proc = subprocess.run(analysis_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if proc.returncode != 0:
            return "error", track, f"Analysis failed:\n{proc.stderr}"

        # Anchor on "input_i" (the first key loudnorm prints) so braces inside
        # metadata blobs in stderr, e.g. TRAKTOR4, cannot capture the match.
        match = re.search(r'\{\s*"input_i"\s*:[\s\S]*?\}', proc.stderr)
        stats = json.loads(match.group(0)) if match else None

        if not stats:
            return "error", track, "Could not parse loudnorm stats."

        # Untouched file stat: used for skipped / off_target outcomes.
        stat_info = {"mtime": track.stat().st_mtime_ns, "size": track.stat().st_size, **params.as_dict()}

        if abs(float(stats["input_i"]) - params.target_lufs) <= params.tolerance:
            return "skipped", track, stat_info

        feasible, reason = linear_feasible(stats, params)
        if not feasible:
            stat_info["note"] = f"skipped: {reason}"
            return "off_target", track, stat_info

        # LRA=50 is the filter's maximum and is inert in linear mode, but
        # loudnorm only takes the linear path when measured_LRA <= LRA, so the
        # target LRA must not be passed here: it would force dynamic mode on
        # every track with wider dynamics than the target.
        loudnorm_filter = (
            f"loudnorm=I={params.target_lufs}:TP={params.true_peak}:LRA=50:"
            f"measured_I={stats['input_i']}:measured_LRA={stats['input_lra']}:"
            f"measured_TP={stats['input_tp']}:measured_thresh={stats['input_thresh']}:"
            f"offset={stats['target_offset']}:print_format=summary"
        )

        temp_file = track.with_name(f".tmp_{track.name}")

        codec_opts = ["-c:a", fmt["codec"]]
        if "b" in fmt: codec_opts.extend(["-b:a", fmt["b"]])
        if "ar" in fmt: codec_opts.extend(["-ar", fmt["ar"]])

        # Map audio only: containers that accept video (m4a) would otherwise
        # pick up an embedded cover-art video stream and fail to encode it.
        # Mutagen re-attaches cover art and tags after the encode.
        metadata_opts = ["-map", "0:a", "-map_metadata", "-1"] if suffix in MUTAGEN_EXTS else ["-map_metadata", "0", "-map", "0:a"]

        encode_cmd = [
            "ffmpeg", "-threads", "1", "-filter_threads", "1",
            "-i", str(track), "-af", loudnorm_filter,
            *codec_opts, *metadata_opts, *fmt.get("extra", []), "-y", str(temp_file)
        ]

        proc2 = subprocess.run(encode_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if proc2.returncode != 0:
            if temp_file.exists(): temp_file.unlink()
            return "error", track, f"Encoding failed:\n{proc2.stderr}"

        if not temp_file.exists():
            return "error", track, "Output file not created."

        # Keep the original if ffmpeg's linear-mode rules change.
        # Never let a destructive result replace the original.
        mode = re.search(r"Normalization Type:\s*(\w+)", proc2.stderr)
        if mode is None or mode.group(1) != "Linear":
            temp_file.unlink()
            stat_info["note"] = (
                f"skipped: loudnorm ran in {mode.group(1) if mode else 'unknown'} mode "
                f"instead of linear - destructive to dynamic range"
            )
            return "off_target", track, stat_info

        if suffix in MUTAGEN_EXTS:
            clone_metadata(track, temp_file, suffix)

        temp_file.replace(track)
        stat_info = {"mtime": track.stat().st_mtime_ns, "size": track.stat().st_size, **params.as_dict()}

        output_i = re.search(r"Output Integrated:\s+([-\d.]+) LUFS", proc2.stderr)
        if output_i and abs(float(output_i.group(1)) - params.target_lufs) > params.tolerance:
            stat_info["note"] = (
                f"output integrated {output_i.group(1)} LUFS "
                f"(target {params.target_lufs} LUFS, tolerance ±{params.tolerance} LUFS)"
            )
            return "off_target", track, stat_info

        return "normalized", track, stat_info

    except FileNotFoundError:
        return "error", track, "ffmpeg was not found in PATH."
    except Exception as e:
        return "error", track, f"Unexpected error: {e}"


def main():
    parser = argparse.ArgumentParser(description="Normalize an audio library to EBU R128 using ffmpeg.")
    parser.add_argument("directory", nargs="?", default=".", help="Music library root.")
    parser.add_argument("--target-lufs", type=float, default=DEFAULT_TARGET_LUFS,
                        help=f"Integrated loudness target in LUFS, EBU R128 (default {DEFAULT_TARGET_LUFS}).")
    parser.add_argument("--true-peak", type=float, default=DEFAULT_TRUE_PEAK,
                        help=f"Maximum true peak in dBTP (default {DEFAULT_TRUE_PEAK}).")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                        help=f"LUFS tolerance for 'already at target' (default {DEFAULT_TOLERANCE}).")
    args = parser.parse_args()

    # ffmpeg loudnorm option ranges (ffmpeg -h filter=loudnorm)
    if not (-70 <= args.target_lufs <= -5):
        parser.error(f"--target-lufs must be within [-70, -5] LUFS, got {args.target_lufs}")
    if not (-9 <= args.true_peak <= 0):
        parser.error(f"--true-peak must be within [-9, 0] dBTP, got {args.true_peak}")
    if args.tolerance <= 0:
        parser.error(f"--tolerance must be positive, got {args.tolerance}")

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"{root} is not a directory.")
        return 1

    params = Params(args.target_lufs, args.true_peak, args.tolerance)

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
        if needs_processing(state.get(rel), stat, params):
            tracks.append(file)

    if not tracks:
        print("Library already normalized." if state else "No supported audio files found.")
        return 0

    workers = max(1, (os.cpu_count() or 1) - 1)
    print(f"Directory : {root}\nTracks    : {len(tracks)}\nWorkers   : {workers}\n"
          f"Target    : {params.target_lufs} LUFS / {params.true_peak} dBTP / "
          f"tolerance ±{params.tolerance} LUFS\n")

    completed, normalized, skipped, off_target, errors = 0, 0, 0, 0, 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(normalize_with_ffmpeg, t, params) for t in tracks]
        for future in concurrent.futures.as_completed(futures):
            completed += 1
            status, path, info = future.result()

            if status in ("skipped", "normalized", "off_target"):
                state[path.relative_to(root).as_posix()] = info
                if status == "skipped":
                    skipped += 1
                elif status == "off_target":
                    off_target += 1
                    with PRINT_LOCK:
                        print(f"\nWARNING: {path}: {info.get('note', 'did not reach target loudness')}")
                else:
                    normalized += 1
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

    print(f"Finished. {normalized} normalized, {skipped} already at target (skipped), "
          f"{off_target} left untouched (see warnings), "
          f"{errors} error(s).")
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
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
