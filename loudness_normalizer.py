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

# Defaults - every setting is a CLI option, and every setting is part of the
# per-track state fingerprint: change any of them and tracks re-encode
# (provided the re-encode would not destroy their dynamic range, see
# linear_feasible).
DEFAULT_TARGET_LUFS = -10.0
DEFAULT_TRUE_PEAK = -1.0
DEFAULT_LRA = 7.0
DEFAULT_TOLERANCE = 0.5

PRINT_LOCK = threading.Lock()


class Params:
    """Normalization settings. as_dict() is the settings half of the
    idempotency fingerprint."""

    __slots__ = ("target_lufs", "true_peak", "lra", "tolerance")

    def __init__(self, target_lufs, true_peak, lra, tolerance):
        self.target_lufs = target_lufs
        self.true_peak = true_peak
        self.lra = lra
        self.tolerance = tolerance

    def as_dict(self):
        return {
            "target_lufs": self.target_lufs,
            "true_peak": self.true_peak,
            "lra": self.lra,
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
    """Idempotency fingerprint: a track needs processing iff it has no state
    entry, its {mtime, size} differs from the recorded one, or the recorded
    entry was produced under different settings than the current params.
    Old entries (no settings keys) therefore become candidates on the first
    run after this change - nothing is assumed to have been re-encoded."""
    if not cached:
        return True
    if cached.get("mtime") != stat.st_mtime_ns or cached.get("size") != stat.st_size:
        return True
    return any(cached.get(key) != value for key, value in params.as_dict().items())


def linear_feasible(stats, params) -> bool:
    """Mirror of ffmpeg's af_loudnorm.c init() linear-mode condition.

    loudnorm normalizes in one of two ways:
      - linear: constant gain, dynamics fully preserved
      - dynamic: applies compression (destructive to dynamic range)
    The filter picks linear iff the gain needed (target_i - input_i) keeps
    the true peak under the target TP and the source LRA does not exceed the
    target LRA; otherwise it silently falls back to dynamic. We refuse to
    compress, so this gate decides whether a track is re-encoded at all."""
    offset = params.target_lufs - float(stats["input_i"])
    offset_tp = float(stats["input_tp"]) + offset
    return offset_tp <= params.true_peak and float(stats["input_lra"]) <= params.lra


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

        match = re.search(r'\{[\s\S]*?"target_offset"[\s\S]*?\}', proc.stderr)
        stats = json.loads(match.group(0)) if match else None

        if not stats:
            return "error", track, "Could not parse loudnorm stats."

        # Current file, untouched: used for skipped / off_target outcomes.
        stat_info = {"mtime": track.stat().st_mtime_ns, "size": track.stat().st_size, **params.as_dict()}

        if abs(float(stats["input_i"]) - params.target_lufs) <= params.tolerance:
            return "skipped", track, stat_info

        if not linear_feasible(stats, params):
            stat_info["note"] = (
                f"skipped: normalizing to {params.target_lufs} LUFS would fall back to "
                f"dynamic mode (destructive to dynamic range)"
            )
            return "off_target", track, stat_info

        loudnorm_filter = (
            f"loudnorm=I={params.target_lufs}:TP={params.true_peak}:LRA={params.lra}:"
            f"measured_I={stats['input_i']}:measured_LRA={stats['input_lra']}:"
            f"measured_TP={stats['input_tp']}:measured_thresh={stats['input_thresh']}:"
            f"offset={stats['target_offset']}:print_format=summary"
        )

        temp_file = track.with_name(f".tmp_{track.name}")

        codec_opts = ["-c:a", fmt["codec"]]
        if "b" in fmt: codec_opts.extend(["-b:a", fmt["b"]])
        if "ar" in fmt: codec_opts.extend(["-ar", fmt["ar"]])

        metadata_opts = ["-map_metadata", "-1"] if suffix in MUTAGEN_EXTS else ["-map_metadata", "0", "-map", "0:a"]

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

        # The gate above mirrors loudnorm's own linear condition, but the
        # filter can still fall back to dynamic (e.g. measured LRA == 0 on
        # constant-level material). Never let a destructive result replace
        # the original: abort the write and keep the old file.
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
                f"(target {params.target_lufs} LUFS, tolerance ±{params.tolerance})"
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
    parser.add_argument("--lra", type=float, default=DEFAULT_LRA,
                        help=f"Target loudness range in LU. Tracks with wider dynamics than this "
                             f"cannot be normalized without compression and are skipped "
                             f"(default {DEFAULT_LRA}).")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                        help=f"LUFS tolerance for 'already at target' (default {DEFAULT_TOLERANCE}).")
    args = parser.parse_args()

    # ffmpeg loudnorm option ranges (ffmpeg -h filter=loudnorm)
    if not (-70 <= args.target_lufs <= -5):
        parser.error(f"--target-lufs must be within [-70, -5] LUFS, got {args.target_lufs}")
    if not (-9 <= args.true_peak <= 0):
        parser.error(f"--true-peak must be within [-9, 0] dBTP, got {args.true_peak}")
    if not (1 <= args.lra <= 50):
        parser.error(f"--lra must be within [1, 50] LU, got {args.lra}")
    if args.tolerance <= 0:
        parser.error(f"--tolerance must be positive, got {args.tolerance}")

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"{root} is not a directory.")
        return 1

    params = Params(args.target_lufs, args.true_peak, args.lra, args.tolerance)

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
          f"LRA {params.lra} LU / ±{params.tolerance}\n")

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
          f"{off_target} left untouched (normalization would be destructive), {errors} error(s).")
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
