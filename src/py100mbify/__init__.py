#!/usr/bin/env python
# ruff: noqa: F541 E701

import argparse
import subprocess
import os
import sys
import shutil
import json
import time
import math
import shlex
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

    # Fast Seeking
    if cfg["start_sec"] > 0:
        cmd.extend(["-ss", f"{cfg['start_sec']:.3f}"])

    cmd.extend(["-i", args_obj.input_file])

    # Precise Trimming
    if cfg["clip_duration"] > 0:
        cmd.extend(["-t", f"{cfg['clip_duration']:.3f}"])

    v_filters = []
    if args_obj.prepend_filters:
        v_filters.append(args_obj.prepend_filters)

    # Burn-in Subtitles with Sync Fix
    if args_obj.hard_sub:
        esc = escape_ffmpeg_path(args_obj.input_file)
        # Shift PTS forward by start_sec to align subtitle stream, then back to 0
        v_filters.append(f"setpts=PTS+({cfg['start_sec']}/TB)")
        v_filters.append(f"subtitles='{esc}'")
        v_filters.append("setpts=PTS-STARTPTS")
        cmd.append("-sn")

    if args_obj.rotate:
        rad = math.radians(args_obj.rotate)
        v_filters.append(f"rotate={rad}:ow=rotw({rad}):oh=roth({rad})")

    if args_obj.speed != 1.0:
        v_filters.append(f"setpts={1 / args_obj.speed}*PTS")

    if args_obj.scale:
        has_manual_scale = args_obj.prepend_filters and "scale" in args_obj.prepend_filters.lower()

        if has_manual_scale:
            print(">>> Warning: Manual scale detected in --prepend-filters while --scale is also set.")
            print(">>> The internal --scale option will be applied AFTER your manual filters.")

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
        cmd.extend(["-flags", "+cgop", "-g", str(int(args_obj.fps or cfg["src_fps"]))])

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

    if args_obj.mute:
        cmd.append("-an")
    else:
        cmd.extend(
            ["-c:a", "libopus", "-b:a", f"{args_obj.audio_bitrate}k", "-ac", "2"]
        )

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

    duration, w, h, fps, audio, is_vfr = get_video_info(args.input_file)

    start_sec = get_time_in_seconds(args.start)
    end_sec = get_time_in_seconds(args.end) if args.end else duration
    clip_duration = max(0, end_sec - start_sec)
    effective_duration = clip_duration / args.speed

    if clip_duration <= 0:
        raise ScriptError(
            "Effective duration is zero or negative. Check your --start and --end parameters."
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

    # Bitrate Calculation
    total_br, video_br = calculate_bitrates(
        args.size, effective_duration, args.audio_bitrate, not (args.mute or not audio)
    )

    safe_name = "".join(c if c.isalnum() else "_" for c in os.path.basename(out_path))
    log_name = f"passlog_{safe_name}_{int(time.time())}"
    log_prefix = os.path.join(out_dir, log_name)
    cfg = {
        "start_sec": start_sec,
        "clip_duration": clip_duration,
        "effective_duration": effective_duration,
        "video_bitrate": video_br,
        "src_w": w,
        "src_h": h,
        "src_fps": fps,
        "log_prefix": log_prefix,
        "out_path": out_path,
    }

    # Build Dynamic Info Strings
    fps_display = f"{fps:.2f} FPS" + (" (VFR detected)" if is_vfr else " (CFR)")

    # Track explicit user overrides
    overrides = []

    if args.start:
        overrides.append(f"Start: {args.start}")
    if args.end:
        overrides.append(f"End: {args.end}")

    if args.fps:
        if abs(args.fps - fps) > 0.01:
            overrides.append(f"Target FPS: {args.fps}")
        else:
            overrides.append(f"Target FPS: {args.fps} (Matches Source - Ignoring)")

    if args.scale: overrides.append(f"Scale: {args.scale}px ({args.scaler or 'auto'})")
    if args.speed != 1.0: overrides.append(f"Speed: {args.speed}x")
    if args.hard_sub: overrides.append("Hard-subs: Enabled")
    if args.proto: overrides.append(f"Mode: Prototype (CRF {args.proto})")

    header = [
        f"Py100mbify Session Started: {start_timestamp}",
        f"Input: {os.path.basename(args.input_file)} ({duration:.2f}s raw)",
        f"Clip Duration: {effective_duration:.2f}s",
        f"Source: {w}x{h} @ {fps_display}",
        f"Target Size: {args.size} MiB",
        f"Settings: {video_br:.2f}k video, {args.audio_bitrate}k audio",
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
        # Cleanup log files even on failure
        if not args.proto:
            for f in [f"{log_prefix}-0.log", f"{log_prefix}-0.log.temp"]:
                if os.path.exists(f):
                    os.remove(f)

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
                    f"(Range: {args.start or '0'}-{args.end or 'EOF'}) "
                    f"-> {final_size:.2f}MB in {str(timedelta(seconds=int(total_elapsed)))} "
                    f"({speed_ratio:.2f}x)\n"
                )
        except IOError:
            pass  # Silently fail if log is unwritable


def main():
    parser = argparse.ArgumentParser(
        prog="py100mbify",
        description="Py100mbify: A high-precision VP9/WebM target-size compressor for Discord and web sharing.",
        epilog="Example: py100mbify input.mp4 --size 50 --start 00:01:30 --hard-sub",
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
        "--start", metavar="TIME", help="Start offset (HH:MM:SS.mmm or seconds)."
    )
    clip_group.add_argument(
        "--end", metavar="TIME", help="End timestamp (HH:MM:SS.mmm or seconds)."
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
        help="Burn-in subtitles from the source file (hardcoded).",
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

    args = parser.parse_args()

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
