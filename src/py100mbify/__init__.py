#!/usr/bin/env python
# ruff: noqa: F541 E701
# Repository: https://github.com/gphg/py100mbify

import argparse
import subprocess
import os
import sys
import shutil
import json
import time
import math
import shlex
import re
from datetime import datetime, timedelta

# --- Script Configuration ---
REQUIRED_COMMANDS = ["ffprobe", "ffmpeg"]
DEFAULT_TARGET_SIZE_MIB = 100
DEFAULT_AUDIO_BITRATE_KBPS = 192
MIN_VIDEO_BITRATE_KBPS = 50
DEFAULT_THREADS = 4
DEFAULT_QUALITY = "best"


class ScriptError(Exception):
    """Custom exception for script errors."""
    pass


def check_required_commands(commands):
    """Check if all required commands are available."""
    for cmd in commands:
        if not shutil.which(cmd):
            raise ScriptError(
                f"Error: Required command '{cmd}' not found. Please install it."
            )


def get_time_in_seconds(time_str):
    """Converts HH:MM:SS.mmm or seconds to float."""
    if not time_str:
        return 0.0
    try:
        return float(time_str)
    except ValueError:
        parts = time_str.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return float(h) * 3600 + float(m) * 60 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return float(m) * 60 + float(s)
        return 0.0


def escape_ffmpeg_path(path):
    """Escapes file path for FFmpeg filter strings (Windows safe)."""
    path = path.replace("\\", "/")
    path = path.replace(":", "\\:")
    path = path.replace("'", "'\\\\''")
    return path


def parse_srt_time(time_str):
    """Converts SRT time format HH:MM:SS,mmm to float seconds."""
    h, m, s_ms = time_str.split(":")
    s, ms = s_ms.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def format_srt_time(seconds):
    """Converts float seconds back to SRT time format HH:MM:SS,mmm."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))

    # Handle overflow cascades securely
    if ms >= 1000:
        s += 1
        ms -= 1000
    if s >= 60:
        m += 1
        s -= 60
    if m >= 60:
        h += 1
        m -= 60

    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def slice_and_shift_srt(input_srt, output_srt, segments):
    """Reads an SRT, slices it according to multiple segments, and recalculates timestamps."""
    with open(input_srt, "r", encoding="utf-8") as f:
        # Standardize newlines before splitting
        content = f.read().replace("\r\n", "\n")

    # Split by double newlines to isolate subtitle blocks
    blocks = re.split(r"\n\s*\n", content.strip())
    new_blocks = []
    sub_index = 1

    for block in blocks:
        lines = block.split("\n")
        if len(lines) < 3:
            continue

        time_line = lines[1]
        match = re.match(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", time_line)
        if not match:
            continue

        start_sec = parse_srt_time(match.group(1))
        end_sec = parse_srt_time(match.group(2))
        text = "\n".join(lines[2:])

        current_offset = 0.0
        for st, en in segments:
            # Find how much this subtitle overlaps with the current segment
            overlap_start = max(start_sec, st)
            overlap_end = min(end_sec, en)

            # If valid overlap exists, keep and shift the timestamp relative to the concatenated file
            if overlap_start < overlap_end:
                new_start = overlap_start - st + current_offset
                new_end = overlap_end - st + current_offset

                new_time_line = f"{format_srt_time(new_start)} --> {format_srt_time(new_end)}"
                new_blocks.append(f"{sub_index}\n{new_time_line}\n{text}")
                sub_index += 1

            current_offset += (en - st)

    with open(output_srt, "w", encoding="utf-8") as f:
        f.write("\n\n".join(new_blocks) + "\n")


def set_process_priority(priority):
    """Sets CPU priority for the current process and children (cross-platform)."""
    if not priority:
        return
    try:
        if sys.platform == "win32":
            import psutil

            p = psutil.Process(os.getpid())
            if priority == "low":
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            elif priority == "high":
                p.nice(psutil.HIGH_PRIORITY_CLASS)
        else:
            if priority == "low":
                os.nice(10)
    except ImportError:
        if priority:
            print(
                ">>> Warning: 'psutil' is required for CPU priority on Windows. Install with 'pip install psutil'."
            )


def get_video_info(input_file):
    """Capture video metadata with Windows-safe encoding."""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            input_file,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
            errors="replace",
        )
        probe = json.loads(result.stdout)
        duration = float(probe["format"].get("duration", 0))

        video_stream = next(
            (s for s in probe["streams"] if s["codec_type"] == "video"), None
        )
        if not video_stream:
            raise ScriptError("No video stream found in input.")

        width, height = video_stream.get("width", 0), video_stream.get("height", 0)

        fps_raw = video_stream.get("r_frame_rate", "0/1").split("/")
        avg_fps_raw = video_stream.get("avg_frame_rate", "0/1").split("/")
        fps = float(fps_raw[0]) / float(fps_raw[1]) if int(fps_raw[1]) > 0 else 30.0
        avg_fps = float(avg_fps_raw[0]) / float(avg_fps_raw[1]) if int(avg_fps_raw[1]) > 0 else 30.0

        audio_streams = [s for s in probe["streams"] if s["codec_type"] == "audio"]

        # If the difference is more than a tiny fraction, it's VFR
        is_vfr = abs(fps - avg_fps) > 0.05

        return duration, width, height, fps, audio_streams, is_vfr
    except Exception as e:
        raise ScriptError(f"ffprobe failed to read file: {e}")


def calculate_bitrates(size_mib, effective_duration, audio_kbps, is_audio_enabled):
    """
    Returns (total_bitrate_kbps, video_bitrate_kbps).
    Includes a 5% safety margin for container overhead.
    """
    target_bits = size_mib * 8 * 1024 * 1024
    total_bitrate = (target_bits / effective_duration) * 0.95 / 1000
    actual_audio = audio_kbps if is_audio_enabled else 0
    video_bitrate = max(MIN_VIDEO_BITRATE_KBPS, total_bitrate - actual_audio)
    return total_bitrate, video_bitrate


def run_ffmpeg_pass(pass_number, args_obj, cfg):
    """Executes a single FFmpeg pass based on provided configuration."""
    cmd = ["ffmpeg", "-hide_banner", "-y", "-nostdin", "-stats"]

    # Input file handling
    if cfg.get("concat_file"):
        cmd.extend(["-f", "concat", "-safe", "0", "-i", cfg["concat_file"]])
    else:
        cmd.extend(["-i", args_obj.input_file])

        # Accurate Seeking for single segment
        single_start = cfg["segments"][0][0]
        single_duration = cfg["segments"][0][1] - single_start

        if single_start > 0:
            cmd.extend(["-ss", f"{single_start:.3f}"])

        # Compare against the raw duration of the source file
        if single_duration > 0 and single_duration < cfg.get("raw_duration", float('inf')):
            cmd.extend(["-t", f"{single_duration:.3f}"])

    v_filters = []
    if args_obj.prepend_filters:
        v_filters.append(args_obj.prepend_filters)

    # Burn-in Subtitles using sliced SRT
    if args_obj.hard_sub and cfg.get("adjusted_srt"):
        # FFmpeg on Windows requires the colon in the drive letter to be escaped
        safe_sub_path = os.path.abspath(cfg["adjusted_srt"]).replace("\\", "/").replace(":", "\\:")
        v_filters.append(f"subtitles='{safe_sub_path}'")
        cmd.append("-sn")

    if args_obj.rotate:
        rad = math.radians(args_obj.rotate)
        v_filters.append(f"rotate={rad}:ow=rotw({rad}):oh=roth({rad})")

    if args_obj.speed != 1.0:
        v_filters.append(f"setpts={1 / args_obj.speed}*PTS")

    if args_obj.scale:
        has_manual_scale = args_obj.prepend_filters and "scale" in args_obj.prepend_filters.lower()

        if has_manual_scale:
            print(
                ">>> Warning: Manual scale detected in --prepend-filters while --scale is also set.")
            print(
                ">>> The internal --scale option will be applied AFTER your manual filters.")

        f = args_obj.scaler or (
            "neighbor" if cfg["src_h"] % args_obj.scale == 0 else "bicubic"
        )
        dim = (
            f"{args_obj.scale}:-2"
            if cfg["src_w"] < cfg["src_h"]
            else f"-2:{args_obj.scale}"
        )
        v_filters.append(f"scale={dim}:flags={f}")

    if args_obj.fps:
        # Check if the requested FPS is significantly different from source
        # Using a small epsilon (0.01) to account for float precision
        if abs(args_obj.fps - cfg["src_fps"]) > 0.01:
            v_filters.append(f"fps={args_obj.fps}")
        else:
            print(f">>> Info: Requested FPS ({args_obj.fps}) matches source. Skipping filter to preserve quality.")

    if args_obj.append_filters:
        v_filters.append(args_obj.append_filters)

    if v_filters:
        cmd.extend(["-vf", ",".join(v_filters)])

    # Codec Settings
    cmd.extend(["-c:v", "libvpx-vp9", "-row-mt", "1"])
    if cfg["effective_duration"] < 10.0:
        # Optimized GOP for short clips
        cmd.extend(["-keyint_min", str(int(args_obj.fps or cfg["src_fps"]))])
        cmd.extend(["-flags", "+cgop", "-g", str(int(args_obj.fps or cfg["src_fps"]))])
    else:
        cmd.extend(["-keyint_min", "150", "-g", "150"])

    if args_obj.target_web:
        cmd.extend(["-pix_fmt", "yuv420p", "-profile:v", "0"])

    if args_obj.proto:
        cmd.extend(
            [
                "-crf",
                str(args_obj.proto),
                "-b:v",
                "0",
                "-quality",
                "realtime",
                "-speed",
                "4",
            ]
        )
    else:
        cmd.extend(
            [
                "-b:v",
                f"{cfg['video_bitrate']:.0f}k",
                "-pass",
                str(pass_number),
                "-passlogfile",
                cfg["log_prefix"],
            ]
        )
        cmd.extend(["-quality", os.environ.get("PY100MBIFY_QUALITY", DEFAULT_QUALITY)])

    # Threading
    cmd.extend(["-threads", os.environ.get("PY100MBIFY_THREADS", str(DEFAULT_THREADS))])

    # Ensure we only grab the primary video stream
    cmd.extend(["-map", "0:v:0"])

    # Ghost Audio Fix & Explicit Stream Mapping
    if args_obj.mute or not cfg["has_audio"]:
        cmd.append("-an")
    else:
        cmd.extend(["-map", "0:a:0"])
        cmd.extend(["-c:a", "libopus", "-b:a", f"{args_obj.audio_bitrate}k", "-ac", "2"])

    out_path = cfg["out_path"]
    if not args_obj.proto and pass_number == 1:
        cmd.extend(["-f", "webm", "NUL" if sys.platform == "win32" else "/dev/null"])
    else:
        if args_obj.keep_metadata:
            cmd.extend(["-map_metadata", "0"])
        cmd.append(out_path)

    if args_obj.print_mode:
        print(f"\n# Pass {pass_number} command:\n{shlex.join(cmd)}")
        return

    label = "Prototype" if args_obj.proto else f"Pass {pass_number}"
    print(f"\n>>> [{datetime.now().strftime('%H:%M:%S')}] Starting {label}...")
    start_t = time.time()
    try:
        subprocess.run(cmd, check=True)
        elapsed = time.time() - start_t
        print(f">>> {label} completed in {elapsed:.2f}s")
    except subprocess.CalledProcessError as e:
        raise ScriptError(f"FFmpeg {label} failed with exit code {e.returncode}")


def compress_video(**kwargs):
    """Core compression logic with improved reporting and modularity."""
    args = argparse.Namespace(**kwargs)

    check_required_commands(REQUIRED_COMMANDS)
    set_process_priority(args.cpu_priority)

    script_start_time = time.time()
    start_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp = int(script_start_time)

    duration, w, h, fps, audio, is_vfr = get_video_info(args.input_file)

    # Build the segment list
    segments = []
    if args.segment:
        for st, en in args.segment:
            segments.append((get_time_in_seconds(st), get_time_in_seconds(en)))
    elif args.start or args.end:
        st = get_time_in_seconds(args.start)
        en = get_time_in_seconds(args.end) if args.end else duration
        segments.append((st, en))
    else:
        segments.append((0.0, duration))

    # Calculate total clip duration for bitrate math
    clip_duration = sum(max(0.0, en - st) for st, en in segments)
    effective_duration = clip_duration / args.speed

    if clip_duration <= 0:
        raise ScriptError(
            "Effective duration is zero or negative. Check your segment parameters."
        )

    if args.output_file:
        out_path = args.output_file
    else:
        base_filename = os.path.splitext(os.path.basename(args.input_file))[0]
        out_path = f"{base_filename}.webm"

    out_dir = os.path.dirname(os.path.abspath(out_path))

    if os.path.abspath(args.input_file) == os.path.abspath(out_path):
        raise ScriptError(
            f"Output path is the same as input path: {out_path}. Please specify a different output name."
        )

    # Subtitle Extraction and Shifting Engine
    adjusted_srt = None
    if args.hard_sub:
        raw_srt = os.path.join(out_dir, f"raw_sub_{timestamp}.srt").replace("\\", "/")
        adjusted_srt = os.path.join(out_dir, f"cut_sub_{timestamp}.srt").replace("\\", "/")

        print(f">>> Info: Extracting primary subtitle track (0:s:0) for processing...")
        ext_cmd = [
            "ffmpeg", "-hide_banner", "-y", "-i", args.input_file,
            "-map", "0:s:0", "-c:s", "srt", raw_srt
        ]
        subprocess.run(ext_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if not os.path.exists(raw_srt) or os.path.getsize(raw_srt) == 0:
            if os.path.exists(raw_srt): os.remove(raw_srt)
            raise ScriptError("Failed to extract subtitles. Does the source have a text subtitle track at 0:s:0?")

        print(">>> Info: Slicing and synchronizing subtitles to match segments...")
        slice_and_shift_srt(raw_srt, adjusted_srt, segments)

        # Cleanup raw extraction
        if os.path.exists(raw_srt):
            os.remove(raw_srt)

    # Concat Demuxer File Generation
    concat_file = None
    if len(segments) > 1:
        concat_file = os.path.join(out_dir, f"concat_{timestamp}.txt").replace("\\", "/")
        with open(concat_file, "w", encoding="utf-8") as f:
            safe_input = args.input_file.replace("'", "'\\''")
            for st, en in segments:
                f.write(f"file '{safe_input}'\n")
                f.write(f"inpoint {st:.3f}\n")
                f.write(f"outpoint {en:.3f}\n")

    # Bitrate Calculation & Audio Evaluation
    has_audio = len(audio) > 0
    total_br, video_br = calculate_bitrates(
        args.size, effective_duration, args.audio_bitrate, not (args.mute or not has_audio)
    )

    safe_name = "".join(c if c.isalnum() else "_" for c in os.path.basename(out_path))
    log_name = f"passlog_{safe_name}_{timestamp}"

    # Windows passlogfile escape bug fix applied here
    log_prefix = os.path.join(out_dir, log_name).replace("\\", "/")

    cfg = {
        "segments": segments,
        "clip_duration": clip_duration,
        "effective_duration": effective_duration,
        "raw_duration": duration,  # ADDED: Store raw duration for accurate -t checking
        "video_bitrate": video_br,
        "src_w": w,
        "src_h": h,
        "src_fps": fps,
        "log_prefix": log_prefix,
        "out_path": out_path,
        "concat_file": concat_file,
        "adjusted_srt": adjusted_srt,
        "has_audio": has_audio,
    }

    # Build Dynamic Info Strings
    fps_display = f"{fps:.2f} FPS" + (" (VFR detected)" if is_vfr else " (CFR)")
    overrides = []

    if args.segment:
        overrides.append(f"Segments: {len(segments)}")
    elif args.start or args.end:
        overrides.append(f"Start: {args.start or '0'} End: {args.end or 'EOF'}")

    if args.fps:
        if abs(args.fps - fps) > 0.01:
            overrides.append(f"Target FPS: {args.fps}")
        else:
            overrides.append(f"Target FPS: {args.fps} (Matches Source - Ignoring)")

    if args.scale: overrides.append(f"Scale: {args.scale}px ({args.scaler or 'auto'})")
    if args.speed != 1.0: overrides.append(f"Speed: {args.speed}x")
    if args.mute or not has_audio: overrides.append("Audio: Muted/None")
    if args.hard_sub: overrides.append("Hard-subs: Enabled")
    if args.proto: overrides.append(f"Mode: Prototype (CRF {args.proto})")

    header = [
        f"Py100mbify Session Started: {start_timestamp}",
        f"Input: {os.path.basename(args.input_file)} ({duration:.2f}s raw)",
        f"Clip Duration: {effective_duration:.2f}s",
        f"Source: {w}x{h} @ {fps_display}",
        f"Target Size: {args.size} MiB",
        f"Settings: {video_br:.2f}k video, {args.audio_bitrate if has_audio and not args.mute else 0}k audio",
        f"Output Path: {out_path}",
    ]

    if overrides:
        header.append(f"Flags:  {', '.join(overrides)}")

    header.append("-" * 40)
    print("\n".join(header))

    try:
        if args.proto:
            run_ffmpeg_pass(1, args, cfg)
        else:
            run_ffmpeg_pass(1, args, cfg)
            run_ffmpeg_pass(2, args, cfg)
    finally:
        # Secure cleanup logic for all temp files
        cleanup_files = []
        if not args.proto:
            cleanup_files.extend([f"{log_prefix}-0.log", f"{log_prefix}-0.log.temp"])
        if cfg.get("concat_file"):
            cleanup_files.append(cfg["concat_file"])
        if cfg.get("adjusted_srt"):
            cleanup_files.append(cfg["adjusted_srt"])

        for f in cleanup_files:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

    # Post-Encoding Analysis
    if not args.print_mode and os.path.exists(out_path):
        end_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_elapsed = time.time() - script_start_time
        final_size = os.path.getsize(out_path) / (1024 * 1024)

        # Calculate encoding efficiency (x-speed)
        # e.g. 1.00x is real-time, 0.5x is half real-time
        speed_ratio = effective_duration / total_elapsed if total_elapsed > 0 else 0

        summary = [
            f"\n--- Final Encoding Summary ---",
            f"Started at:      {start_timestamp}",
            f"Finished at:     {end_timestamp}",
            f"Total Wall Time: {str(timedelta(seconds=int(total_elapsed)))}",
            f"Encoding Speed:  {speed_ratio:.4f}x real-time",
            f"Target Size:     {args.size} MiB",
            f"Result Size:     {final_size:.2f} MiB ({(final_size / args.size) * 100:.1f}% of target)",
            f"Output File:     {out_path}",
            "---" * 10,
        ]

        summary_text = "\n".join(summary)
        print(summary_text)

        # Persistent Log: Write to a history file so you don't lose data if the terminal closes
        try:
            with open("py100mbify_history.log", "a", encoding="utf-8") as f:
                f.write(
                    f"[{start_timestamp}] COMPLETED: {os.path.basename(args.input_file)} "
                    f"(Segments: {len(segments)}) "
                    f"-> {final_size:.2f}MB in {str(timedelta(seconds=int(total_elapsed)))} "
                    f"({speed_ratio:.2f}x)\n"
                )
        except IOError:
            pass


def sanitize_input_args(args):
    """
    Strips leading/trailing whitespace and removes invisible control characters
    from CLI arguments to prevent silent parsing failures (especially from CSVs).
    """
    cleaned = []
    # Regex for zero-width spaces, BOM, and unexpected control chars.
    # Avoids stripping standard visible ASCII, newlines (\n), or valid UTF-8.
    invisible_chars = re.compile(r'[\u200b\u200c\u200d\u2060\ufeff\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
    for arg in args:
        # Remove invisible characters entirely
        clean_arg = invisible_chars.sub('', arg)
        # Strip standard leading/trailing whitespace (\r, \n, \t, spaces)
        clean_arg = clean_arg.strip()
        cleaned.append(clean_arg)
    return cleaned


def main():
    parser = argparse.ArgumentParser(
        prog="py100mbify",
        description="Py100mbify: A high-precision VP9/WebM target-size compressor for Discord and web sharing.",
        epilog="Example: py100mbify input.mp4 --size 50 --segment 00:01:30 00:05:00 --hard-sub",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # --- Positionals ---
    parser.add_argument("input_file", help="Path to the source video file.")
    parser.add_argument(
        "output_file",
        nargs="?",
        help="Output path. Defaults to [input_filename].webm in the CURRENT working directory.",
    )

    # --- Target Constraints ---
    target_group = parser.add_argument_group("Target Options")
    target_group.add_argument(
        "--size",
        type=float,
        default=100.0,
        metavar="MiB",
        help="Target file size in MiB.",
    )
    target_group.add_argument(
        "--audio-bitrate",
        type=int,
        default=192,
        metavar="kbps",
        help="Audio bitrate for the libopus stream.",
    )
    target_group.add_argument(
        "--mute", action="store_true", help="Strip all audio tracks from the output."
    )

    # --- Clipping & Transformation ---
    clip_group = parser.add_argument_group("Clipping & Transformation")
    clip_group.add_argument(
        "--segment",
        action="append",
        nargs=2,
        metavar=("START", "END"),
        help="Specify multiple segments to keep. Example: --segment 00:00 01:30 --segment 02:45 05:00",
    )
    clip_group.add_argument(
        "--start", metavar="TIME", help="Start offset (HH:MM:SS.mmm or seconds). (Fallback for single cut)"
    )
    clip_group.add_argument(
        "--end", metavar="TIME", help="End timestamp (HH:MM:SS.mmm or seconds). (Fallback for single cut)"
    )
    clip_group.add_argument(
        "--speed",
        type=float,
        default=1.0,
        metavar="VAL",
        help="Playback speed multiplier (e.g., 2.0 for double speed).",
    )
    clip_group.add_argument(
        "--fps", type=float, metavar="VAL", help="Force a specific output frame rate."
    )
    clip_group.add_argument(
        "--scale",
        type=int,
        metavar="PX",
        help="Scale the short/long side to this pixel value (maintains aspect ratio).",
    )
    clip_group.add_argument(
        "--scaler",
        choices=["neighbor", "bicubic", "lanczos"],
        help="The scaling algorithm to use.",
    )
    clip_group.add_argument(
        "--rotate", type=float, metavar="DEG", help="Rotate video clockwise by degrees."
    )

    # --- Video Quality & Filters ---
    quality_group = parser.add_argument_group("Quality & Filtering")
    quality_group.add_argument(
        "--hard-sub",
        action="store_true",
        help="Burn-in subtitles from the primary source track (0:s:0).",
    )
    quality_group.add_argument(
        "--target-web",
        action="store_true",
        help="Optimize for web streaming (yuv420p, profile 0).",
    )
    quality_group.add_argument(
        "--keep-metadata",
        action="store_true",
        help="Copy global metadata from source to output.",
    )
    quality_group.add_argument(
        "--prepend-filters",
        metavar="STR",
        help="FFmpeg video filters to apply BEFORE internal scaling/subtitles.",
    )
    quality_group.add_argument(
        "--append-filters",
        metavar="STR",
        help="FFmpeg video filters to apply AFTER internal logic.",
    )

    # --- Execution Control ---
    exec_group = parser.add_argument_group("Execution Control")
    exec_group.add_argument(
        "--cpu-priority",
        choices=["low", "high"],
        help="Set process niceness (requires psutil on Windows).",
    )
    exec_group.add_argument(
        "--proto",
        nargs="?",
        const=30,
        type=int,
        metavar="CRF",
        help="Perform a fast single-pass 'prototype' encode with given CRF.",
    )
    exec_group.add_argument(
        "--print",
        dest="print_mode",
        action="store_true",
        help="Print the FFmpeg commands to console instead of running them.",
    )

    # Process and sanitize sys.argv before argparse parses it
    clean_sys_argv = sanitize_input_args(sys.argv[1:])
    args = parser.parse_args(clean_sys_argv)

    try:
        compress_video(**vars(args))
    except ScriptError as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
