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
REQUIRED_COMMANDS = ['ffprobe', 'ffmpeg']
DEFAULT_TARGET_SIZE_MIB = 100
DEFAULT_AUDIO_BITRATE_KBPS = 192
MIN_VIDEO_BITRATE_KBPS = 50
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
    """Converts HH:MM:SS.mmm or seconds to float."""
    if not time_str: return 0.0
    try:
        return float(time_str)
    except ValueError:
        parts = time_str.split(':')
        if len(parts) == 3:
            h, m, s = parts
            return float(h) * 3600 + float(m) * 60 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return float(m) * 60 + float(s)
        return 0.0

def escape_ffmpeg_path(path):
    """Escapes file path for FFmpeg filter strings (Windows safe)."""
    path = path.replace('\\', '/')
    path = path.replace(':', '\\:')
    path = path.replace("'", "'\\\\''")
    return path

def set_process_priority(priority):
    """Sets CPU priority for the current process and children."""
    if not priority: return
    try:
        if sys.platform == 'win32':
            import psutil
            p = psutil.Process(os.getpid())
            if priority == 'low':
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            elif priority == 'high':
                p.nice(psutil.HIGH_PRIORITY_CLASS)
        else:
            if priority == 'low':
                os.nice(10)
    except ImportError:
        # psutil not available on Windows, skip silently or log
        pass

def get_video_info(input_file):
    """Capture video metadata with Windows-safe encoding."""
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', input_file]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding='utf-8', errors='replace')
        probe = json.loads(result.stdout)
        duration = float(probe['format'].get('duration', 0))

        video_stream = next((s for s in probe['streams'] if s['codec_type'] == 'video'), None)
        if not video_stream:
            raise ScriptError("No video stream found in input.")

        width = video_stream.get('width', 0)
        height = video_stream.get('height', 0)
        fps_raw = video_stream.get('r_frame_rate', '0/1').split('/')
        fps = float(fps_raw[0]) / float(fps_raw[1]) if int(fps_raw[1]) > 0 else 30.0
        audio_streams = [s for s in probe['streams'] if s['codec_type'] == 'audio']

        return duration, width, height, fps, audio_streams
    except Exception as e:
        raise ScriptError(f"ffprobe failed to read file: {e}")

def run_ffmpeg_pass(pass_number, args, cfg):
    """Constructs and executes the FFmpeg command for a specific pass."""
    cmd = ['ffmpeg', '-hide_banner', '-y', '-nostdin', '-stats']

    # Fast Seeking (Pre-input)
    if cfg['start_sec'] > 0:
        cmd.extend(['-ss', f"{cfg['start_sec']:.3f}"])

    cmd.extend(['-i', args.input_file])

    # Precise Trimming (Post-input)
    if cfg['clip_duration'] > 0:
        cmd.extend(['-t', f"{cfg['clip_duration']:.3f}"])

    # Video Filter Construction
    v_filters = []
    if args.prepend_filters: v_filters.append(args.prepend_filters)

    if args.hard_sub:
        esc = escape_ffmpeg_path(args.input_file)
        # Burn subs requires re-aligning PTS if we used -ss
        v_filters.append(f"setpts=PTS+({cfg['start_sec']}/TB)")
        v_filters.append(f"subtitles='{esc}'")
        v_filters.append("setpts=PTS-STARTPTS")
        cmd.append('-sn')

    if args.rotate:
        rad = math.radians(args.rotate)
        v_filters.append(f"rotate={rad}:ow=rotw({rad}):oh=roth({rad})")

    if args.speed != 1.0:
        v_filters.append(f"setpts={1/args.speed}*PTS")

    if args.scale:
        # Automatic scaler selection: use neighbor for integer scaling
        f = args.scaler or ('neighbor' if cfg['src_h'] % args.scale == 0 else 'bicubic')
        if cfg['src_w'] < cfg['src_h']:
            v_filters.append(f"scale={args.scale}:-2:flags={f}")
        else:
            v_filters.append(f"scale=-2:{args.scale}:flags={f}")

    if args.fps: v_filters.append(f"fps={args.fps}")
    if args.append_filters: v_filters.append(args.append_filters)

    if v_filters:
        cmd.extend(['-vf', ','.join(v_filters)])

    # Video Codec Settings (VP9)
    cmd.extend(['-c:v', 'libvpx-vp9', '-row-mt', '1'])

    # GOP Optimization for short clips
    if cfg['effective_duration'] < 10.0:
        gop_size = int(args.fps or cfg['fps'])
        cmd.extend(['-flags', '+cgop', '-g', str(gop_size)])

    if args.target_web:
        cmd.extend(['-pix_fmt', 'yuv420p', '-profile:v', '0'])

    if args.proto:
        cmd.extend(['-crf', str(args.proto), '-b:v', '0', '-quality', 'realtime', '-speed', '4'])
    else:
        cmd.extend(['-b:v', f"{cfg['video_bitrate']:.0f}k", '-pass', str(pass_number), '-passlogfile', cfg['log_prefix']])
        cmd.extend(['-quality', os.environ.get('PY100MBIFY_QUALITY', DEFAULT_QUALITY)])

    # Threading
    threads = os.environ.get('PY100MBIFY_THREADS', str(DEFAULT_THREADS))
    cmd.extend(['-threads', threads])

    if args.mute:
        cmd.append('-an')
    else:
        cmd.extend(['-c:a', 'libopus', '-b:a', f"{args.audio_bitrate}k"])

    out_path = args.output_file or f"{os.path.splitext(args.input_file)[0]}.webm"
    if not args.proto and pass_number == 1:
        cmd.extend(['-f', 'webm', 'NUL' if sys.platform == 'win32' else '/dev/null'])
    else:
        if args.keep_metadata:
            cmd.extend(['-map_metadata', '0'])
        cmd.append(out_path)

    if args.print_mode:
        print(f"\n# Pass {pass_number} command:")
        print(shlex.join(cmd))
        return

    label = "Prototype Pass" if args.proto else f"Pass {pass_number}"
    print(f"\n>>> Starting {label}...")
    start_t = time.time()

    try:
        subprocess.run(cmd, check=True)
        elapsed = time.time() - start_t
        print(f">>> {label} finished in {elapsed:.2f}s")
    except subprocess.CalledProcessError as e:
        raise ScriptError(f"FFmpeg failed (Exit Code {e.returncode}) during {label}")

def main():
    parser = argparse.ArgumentParser(description='Py100mbify: VP9 Target-Size Compressor')
    parser.add_argument('input_file', help='Input video file')
    parser.add_argument('output_file', nargs='?', help='Output WebM file')
    parser.add_argument('--size', type=float, default=100.0, help='Target size in MiB')
    parser.add_argument('--audio-bitrate', type=int, default=192)
    parser.add_argument('--mute', action='store_true')
    parser.add_argument('--speed', type=float, default=1.0)
    parser.add_argument('--start', help='Start time (HH:MM:SS or seconds)')
    parser.add_argument('--end', help='End time (HH:MM:SS or seconds)')
    parser.add_argument('--fps', type=float)
    parser.add_argument('--scale', type=int)
    parser.add_argument('--scaler', choices=['neighbor', 'bicubic', 'lanczos'])
    parser.add_argument('--rotate', type=float)
    parser.add_argument('--keep-metadata', action='store_true')
    parser.add_argument('--hard-sub', action='store_true')
    parser.add_argument('--target-web', action='store_true')
    parser.add_argument('--cpu-priority', choices=['low', 'high'])
    parser.add_argument('--prepend-filters')
    parser.add_argument('--append-filters')
    parser.add_argument('--proto', nargs='?', const=30, type=int, help='Fast CRF pass')
    parser.add_argument('--print', dest='print_mode', action='store_true')

    args = parser.parse_args()

    try:
        check_required_commands(REQUIRED_COMMANDS)
        set_process_priority(args.cpu_priority)

        script_start_time = time.time()
        duration, w, h, fps, audio = get_video_info(args.input_file)

        start_sec = get_time_in_seconds(args.start)
        end_sec = get_time_in_seconds(args.end) if args.end else duration
        clip_duration = max(0, end_sec - start_sec)
        effective_duration = clip_duration / args.speed

        if clip_duration <= 0:
            raise ScriptError("Invalid duration: Start time is after End time or exceeds file length.")

        # Bitrate Calculation with 5% safety margin
        target_bits = args.size * 8 * 1024 * 1024
        total_bitrate = (target_bits / effective_duration) * 0.95 / 1000
        audio_bitrate = 0 if (args.mute or not audio) else args.audio_bitrate
        video_bitrate = max(MIN_VIDEO_BITRATE_KBPS, total_bitrate - audio_bitrate)

        log_prefix = f"passlog_{int(time.time())}"
        cfg = {
            'start_sec': start_sec, 'clip_duration': clip_duration, 'effective_duration': effective_duration,
            'video_bitrate': video_bitrate, 'src_w': w, 'src_h': h, 'fps': fps,
            'log_prefix': log_prefix
        }

        # Header Info
        print(f"Py100mbify - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Processing: {args.input_file}")
        print(f"Target: {args.size} MiB | Duration: {effective_duration:.2f}s")
        if not args.proto:
            print(f"Bitrate: {video_bitrate:.2f}k (Video) + {audio_bitrate}k (Audio)")

        if args.proto:
            run_ffmpeg_pass(1, args, cfg)
        else:
            run_ffmpeg_pass(1, args, cfg)
            run_ffmpeg_pass(2, args, cfg)
            # Cleanup logs
            for f in [f"{log_prefix}-0.log", f"{log_prefix}-0.log.temp"]:
                if os.path.exists(f): os.remove(f)

        if not args.print_mode:
            out_path = args.output_file or f"{os.path.splitext(args.input_file)[0]}.webm"
            final_size = os.path.getsize(out_path) / (1024 * 1024)
            total_time = time.time() - script_start_time

            print(f"\n--- Summary ---")
            print(f"Result: {out_path}")
            print(f"Final Size: {final_size:.2f} MiB (Diff: {final_size - args.size:+.2f} MiB)")
            print(f"Total Execution Time: {str(timedelta(seconds=int(total_time)))}")

    except ScriptError as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
