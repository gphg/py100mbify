#!/usr/bin/env python
# ruff: noqa: F541

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
REQUIRED_COMMANDS = ['ffprobe', 'ffmpeg']
DEFAULT_TARGET_SIZE_MIB = 100  # Default target output size in MiB
DEFAULT_AUDIO_BITRATE_KBPS = 192 # Default audio bitrate in kbps
MIN_VIDEO_BITRATE_KBPS = 50

# Default values for configurable FFmpeg options
DEFAULT_THREADS = 4
DEFAULT_QUALITY = 'best'

class ScriptError(Exception):
    """Custom exception for script errors."""
    pass

def check_required_commands(commands):
    """Check if all required commands are available."""
    for cmd in commands:
        if not shutil.which(cmd):
            raise ScriptError(f"Error: Required command '{cmd}' not found. Please install it.")

def get_time_in_seconds(time_str):
    """Converts HH:MM:SS.mmm string or a numeric string to total seconds."""
    if not time_str:
        return 0.0
    try:
        # Check if it's a numeric string (seconds)
        return float(time_str)
    except ValueError:
        # Assume it's an FFmpeg timecode string (HH:MM:SS.mmm)
        parts = time_str.split(':')
        if len(parts) == 3:
            h, m, s = parts
            return float(h) * 3600 + float(m) * 60 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return float(m) * 60 + float(s)
        else:
            return 0.0

def escape_ffmpeg_path(path):
    """
    Escapes a file path for use in an FFmpeg filter string.
    Crucial for Windows paths with backslashes and drive colons.
    """
    path = path.replace('\\', '/')
    path = path.replace(':', '\\:')
    path = path.replace("'", "'\\\\''")
    return path

def get_video_filter(src_w, src_h, target_scale, manual_scaler=None):
    """Smart scaling that respects orientation (Landscape vs Portrait)."""
    if manual_scaler:
        flags = manual_scaler
    else:
        is_integer_scale = (src_h % target_scale == 0) if src_h > target_scale else False
        flags = 'neighbor' if is_integer_scale else 'bicubic'

    if src_w < src_h:
        scale_str = f'scale={target_scale}:-2:flags={flags}'
    else:
        scale_str = f'scale=-2:{target_scale}:flags={flags}'
    return scale_str

def get_video_info(input_file):
    """Use ffprobe to get video duration, res, FPS, and audio streams."""
    try:
        cmd = [
            'ffprobe',
            '-v', 'quiet',
            '-print_format', 'json',
            '-show_format',
            '-show_streams',
            input_file
        ]
        # FIX: Explicitly set UTF-8 encoding for reliable output capture on Windows
        result = subprocess.run(cmd,
            capture_output=True,
            text=True,
            check=True,
            encoding='utf-8',
            errors='replace'
        )
        probe_output = json.loads(result.stdout)

        duration_seconds = float(probe_output['format']['duration'])

        video_stream = next((s for s in probe_output['streams'] if s['codec_type'] == 'video'), None)
        if not video_stream:
            video_width = 0
            video_height = 0
            video_fps = 0.0
        else:
            video_width = video_stream['width']
            video_height = video_stream['height']
            fps_string = video_stream.get('r_frame_rate', '0/1')
            num, den = map(int, fps_string.split('/'))
            video_fps = float(num) / float(den) if den != 0 else 0

        audio_streams = [s for s in probe_output['streams'] if s['codec_type'] == 'audio']
        return duration_seconds, audio_streams, video_width, video_height, video_fps

    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
        raise ScriptError(f"Error: ffprobe failed to get video information. Details: {e}")

def run_ffmpeg_pass(pass_number, input_file, output_file, effective_duration_seconds, clip_duration_seconds,
                    target_video_bitrate_kbps, audio_bitrate, mute, speed, start, end,
                    fps, scale_filter, cpu_priority, prepend_filters, append_filters, pass_log_file,
                    threads, quality, rotate, keep_metadata, hard_sub=False, target_web=False, proto=False,
                    print_mode=False):
    """Refactored FFmpeg pass runner with timing and hard-sub sync fixes."""
    if proto and pass_number == 1:
        return

    cmd = ['ffmpeg', '-hide_banner', '-y', '-nostdin', '-stats']

    # Timing logic
    start_sec = get_time_in_seconds(start)
    if start_sec > 0:
        cmd.extend(['-ss', f'{start_sec:.3f}'])

    cmd.extend(['-i', input_file])

    # Duration of the clip to extract
    if clip_duration_seconds:
        cmd.extend(['-t', f'{clip_duration_seconds:.3f}'])

    video_filters = []
    if prepend_filters:
        video_filters.append(prepend_filters)

    if hard_sub:
        # Sync Fix: shift PTS forward to match subtitles, burn, then shift back to 0
        escaped_input = escape_ffmpeg_path(input_file)
        video_filters.append(f"setpts=PTS+({start_sec}/TB)")
        video_filters.append(f"subtitles='{escaped_input}'")
        video_filters.append("setpts=PTS-STARTPTS")
        cmd.append('-sn')

    if rotate:
        rad = math.radians(rotate)
        video_filters.append(f'rotate={rad}:ow=rotw({rad}):oh=roth({rad})')

    if speed != 1.0:
        video_filters.append(f'setpts={1/speed}*PTS')

    if scale_filter:
        video_filters.append(scale_filter)

    if fps:
        video_filters.append(f'fps={fps}')

    if append_filters:
        video_filters.append(append_filters)

    if video_filters:
        cmd.extend(['-vf', ','.join(video_filters)])

    cmd.extend(['-c:v', 'libvpx-vp9', '-row-mt', '1'])

    # GOP Optimization for loops
    if effective_duration_seconds < 10.0:
        gop_size = int(fps) if fps else 30
        cmd.extend(['-flags', '+cgop', '-g', str(gop_size)])

    if target_web:
        cmd.extend(['-pix_fmt', 'yuv420p', '-profile:v', '0'])

    if proto:
        cmd.extend([
            '-crf', str(proto), '-b:v', '0',
            '-quality', 'realtime', '-speed', '4',
            '-threads', str(threads)
        ])
    else:
        cmd.extend(['-b:v', f'{target_video_bitrate_kbps}k'])
        cmd.extend(['-pass', str(pass_number), '-passlogfile', pass_log_file])
        if pass_number == 1:
            cmd.extend(['-f', 'webm'])
        else:
            if keep_metadata:
                cmd.extend(['-map_metadata', '0'])
            cmd.extend(['-quality', quality, '-threads', str(threads)])

    if mute:
        cmd.append('-an')
    else:
        cmd.extend(['-c:a', 'libopus', '-b:a', f'{audio_bitrate}k'])

    if not proto and pass_number == 1:
        cmd.append(os.devnull)
    else:
        cmd.append(output_file)

    if print_mode:
        print(f"\n# --- FFmpeg Command (Pass {pass_number}) ---")
        print(shlex.join(cmd))
        return

    print(f"\n--- Starting Pass {pass_number} ---")
    pass_start_time = time.time()
    try:
        subprocess.run(cmd, check=True)
        elapsed = time.time() - pass_start_time
        mins, secs = divmod(int(elapsed), 60)
        label = "Prototype Pass" if proto else f"Pass {pass_number}"
        print(f"\n--- FFmpeg {label} completed in {mins}m {secs}s ---")
    except subprocess.CalledProcessError as e:
        raise ScriptError(f"FFmpeg failed at pass {pass_number} (Exit Code: {e.returncode}).")
    except KeyboardInterrupt:
        sys.exit(1)

def calculate_bitrates(size, effective_duration_seconds, audio_bitrate, is_audio_enabled):
    """Calculates target video bitrate with overhead buffer."""
    if effective_duration_seconds <= 0:
         raise ScriptError("Error: Effective duration for bitrate calculation is zero or negative.")
    target_size_bits = size * 8 * 1024 * 1024
    target_total_bitrate_kbps = (target_size_bits / effective_duration_seconds) * 0.95 / 1000
    audio_bitrate_to_subtract_kbps = audio_bitrate if is_audio_enabled else 0
    target_video_bitrate_kbps = target_total_bitrate_kbps - audio_bitrate_to_subtract_kbps
    if target_video_bitrate_kbps <= MIN_VIDEO_BITRATE_KBPS:
        target_video_bitrate_kbps = MIN_VIDEO_BITRATE_KBPS
    return target_total_bitrate_kbps, target_video_bitrate_kbps

def compress_video(input_file, output_file=None, size=float(DEFAULT_TARGET_SIZE_MIB),
                    audio_bitrate=DEFAULT_AUDIO_BITRATE_KBPS, mute=False, speed=1.0,
                    start=None, end=None, fps=None, scale=None, scaler=None, cpu_priority=None,
                    prepend_filters=None, append_filters=None, rotate=None, keep_metadata=False,
                    hard_sub=False, target_web=False, info_detail=False, proto=False,
                    print_mode=False):
    """Core logic for orchestrating the compression process."""
    try:
        check_required_commands(REQUIRED_COMMANDS)
        script_start_time = time.time()

        if output_file is None:
            base, _ = os.path.splitext(os.path.basename(input_file))
            output_file = f"{base}.webm"

        abs_input = os.path.abspath(input_file)
        abs_output = os.path.abspath(output_file)
        if abs_input == abs_output:
            raise ScriptError("Input and output paths are the same. Overwrite prevention triggered.")

        threads = int(os.environ.get('PY100MBIFY_THREADS', DEFAULT_THREADS))
        quality = os.environ.get('PY100MBIFY_QUALITY', DEFAULT_QUALITY)

        if proto:
            proto = max(30, min(int(proto) if not isinstance(proto, bool) else 30, 63))

        duration_seconds, audio_streams, video_width, video_height, video_fps = get_video_info(input_file)

        start_sec = get_time_in_seconds(start)
        end_sec = get_time_in_seconds(end) if end else duration_seconds
        clip_duration_seconds = end_sec - start_sec

        if clip_duration_seconds <= 0:
            raise ScriptError("Calculated clip duration is zero or negative.")

        effective_duration_seconds = clip_duration_seconds / speed
        is_audio_enabled = not mute and audio_streams

        target_total_bitrate_kbps, target_video_bitrate_kbps = calculate_bitrates(
            size, effective_duration_seconds, audio_bitrate, is_audio_enabled
        )

        log_base_name = os.path.splitext(os.path.basename(output_file))[0]
        pass_log_file = os.path.join(os.path.dirname(output_file) or os.getcwd(), f"{log_base_name}_passlog")

        scale_filter = get_video_filter(video_width, video_height, scale, scaler) if scale else None

        if info_detail:
            print(f"--- Compression Summary ---")
            print(f"Input: {input_file} | Output: {output_file}")
            print(f"Clip Duration: {clip_duration_seconds:.2f}s | Result Duration: {effective_duration_seconds:.2f}s")
            print(f"Target Size: {size} MiB | Calculated Video Bitrate: {target_video_bitrate_kbps:.2f} kbps")

        passes = [2] if proto else [1, 2]
        for p in passes:
            run_ffmpeg_pass(p, input_file, output_file, effective_duration_seconds, clip_duration_seconds, 
                            target_video_bitrate_kbps, audio_bitrate, mute, speed, start, end, fps, 
                            scale_filter, cpu_priority, prepend_filters, append_filters, pass_log_file, 
                            threads, quality, rotate, keep_metadata, hard_sub, target_web, proto, print_mode)

        if print_mode: return output_file, 0.0

        # Cleanup
        if not proto:
            for log_file in [f'{pass_log_file}-0.log', f'{pass_log_file}-0.log.temp']:
                if os.path.exists(log_file): os.remove(log_file)

        final_size_mib = os.path.getsize(output_file) / (1024 * 1024)
        print(f"\nCompleted! Final Size: {final_size_mib:.2f} MiB")
        return output_file, final_size_mib

    except ScriptError as e:
        print(f"Error: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Unexpected Error: {e}", file=sys.stderr)
    return None, None

def main():
    parser = argparse.ArgumentParser(description='Compresses video to target size.')
    parser.add_argument('input_file', help='Path to input video.')
    parser.add_argument('output_file', nargs='?', help='Output WebM path.')
    parser.add_argument('--size', type=float, default=DEFAULT_TARGET_SIZE_MIB, help='Target size in MiB.')
    parser.add_argument('--audio-bitrate', type=int, default=DEFAULT_AUDIO_BITRATE_KBPS)
    parser.add_argument('--mute', action='store_true')
    parser.add_argument('--speed', type=float, default=1.0)
    parser.add_argument('--start', help='Start time.')
    parser.add_argument('--end', help='End time.')
    parser.add_argument('--fps', type=float)
    parser.add_argument('--scale', type=int, help='Smallest dimension scale.')
    parser.add_argument('--scaler', choices=['neighbor', 'bicubic', 'lanczos'])
    parser.add_argument('--rotate', type=float)
    parser.add_argument('--keep-metadata', action='store_true')
    parser.add_argument('--hard-sub', action='store_true', help='Burn subtitles with sync fix.')
    parser.add_argument('--target-web', action='store_true', help='Force VP9 Profile 0.')
    parser.add_argument('--cpu-priority', choices=['low', 'high'])
    parser.add_argument('--prepend-filters')
    parser.add_argument('--append-filters')
    parser.add_argument('--proto', nargs='?', const=30, type=int, help='Fast single-pass.')
    parser.add_argument('--print', dest='print_mode', action='store_true', help='Print commands.')
    args = parser.parse_args()
    compress_video(info_detail=True, **vars(args))

if __name__ == '__main__':
    main()
