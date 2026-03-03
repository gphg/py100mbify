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
            raise ScriptError(f"Error: Required command '{cmd}' not found.")

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
        fps = float(fps_raw[0]) / float(fps_raw[1]) if int(fps_raw[1]) > 0 else 0
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
        # Shift PTS forward to sync subtitle timestamps, burn, then reset to 0
        v_filters.append(f"setpts=PTS+({cfg['start_sec']}/TB)")
        v_filters.append(f"subtitles='{esc}'")
        v_filters.append("setpts=PTS-STARTPTS")
        cmd.append('-sn') # Ignore internal subs since we are burning

    if args.rotate:
        rad = math.radians(args.rotate)
        v_filters.append(f"rotate={rad}:ow=rotw({rad}):oh=roth({rad})")

    if args.speed != 1.0:
        v_filters.append(f"setpts={1/args.speed}*PTS")

    if args.scale:
        f = args.scaler or ('neighbor' if cfg['src_h'] % args.scale == 0 else 'bicubic')
        # Handle portrait vs landscape scaling
        if cfg['src_w'] < cfg['src_h']:
            v_filters.append(f"scale={args.scale}:-2:flags={f}")
        else:
            v_filters.append(f"scale=-2:{args.scale}:flags={f}")

    if args.fps: v_filters.append(f"fps={args.fps}")
    if args.append_filters: v_filters.append(args.append_filters)

    if v_filters:
        cmd.extend(['-vf', ','.join(v_filters)])

    # Video Codec Settings
    cmd.extend(['-c:v', 'libvpx-vp9', '-row-mt', '1'])

    # GOP Optimization for short loops
    if cfg['effective_duration'] < 10.0:
        gop_size = int(args.fps or cfg['fps'] or 30)
        cmd.extend(['-flags', '+cgop', '-g', str(gop_size)])

    if args.target_web:
        cmd.extend(['-pix_fmt', 'yuv420p', '-profile:v', '0'])

    if args.proto:
        crf = args.proto
        cmd.extend(['-crf', str(crf), '-b:v', '0', '-quality', 'realtime', '-speed', '4'])
    else:
        cmd.extend(['-b:v', f"{cfg['video_bitrate']:.0f}k", '-pass', str(pass_number), '-passlogfile', cfg['log_prefix']])
        cmd.extend(['-quality', os.environ.get('PY100MBIFY_QUALITY', DEFAULT_QUALITY)])

    cmd.extend(['-threads', os.environ.get('PY100MBIFY_THREADS', str(DEFAULT_THREADS))])

    # Audio Settings
    if args.mute:
        cmd.append('-an')
    else:
        cmd.extend(['-c:a', 'libopus', '-b:a', f"{args.audio_bitrate}k"])

    # Output Handling
    out_path = args.output_file or f"{os.path.splitext(args.input_file)[0]}.webm"
    if not args.proto and pass_number == 1:
        cmd.extend(['-f', 'webm', os.devnull])
    else:
        if args.keep_metadata:
            cmd.extend(['-map_metadata', '0'])
        cmd.append(out_path)

    if args.print_mode:
        print(f"\n# --- FFmpeg Command (Pass {pass_number}) ---")
        print(shlex.join(cmd))
        return

    # Process Priority
    pre_exec = None
    if args.cpu_priority == 'low' and sys.platform != 'win32':
        pre_exec = lambda: os.nice(10)

    print(f"\n--- Starting Pass {pass_number} ---")
    try:
        subprocess.run(cmd, check=True, preexec_fn=pre_exec)
    except subprocess.CalledProcessError as e:
        raise ScriptError(f"FFmpeg failed at pass {pass_number} (Exit Code: {e.returncode})")

def main():
    parser = argparse.ArgumentParser(description='Compress video to target size with VP9.')
    parser.add_argument('input_file', help='Path to input video')
    parser.add_argument('output_file', nargs='?', help='Output WebM path')
    parser.add_argument('--size', type=float, default=100.0, help='Target size in MiB')
    parser.add_argument('--audio-bitrate', type=int, default=192)
    parser.add_argument('--mute', action='store_true')
    parser.add_argument('--speed', type=float, default=1.0)
    parser.add_argument('--start', help='Start time (HH:MM:SS or seconds)')
    parser.add_argument('--end', help='End time (HH:MM:SS or seconds)')
    parser.add_argument('--fps', type=float)
    parser.add_argument('--scale', type=int, help='Smallest dimension scale target')
    parser.add_argument('--scaler', choices=['neighbor', 'bicubic', 'lanczos'])
    parser.add_argument('--rotate', type=float, help='Degrees to rotate')
    parser.add_argument('--keep-metadata', action='store_true')
    parser.add_argument('--hard-sub', action='store_true', help='Burn subs with sync fix')
    parser.add_argument('--target-web', action='store_true', help='Force VP9 Profile 0/yuv420p')
    parser.add_argument('--cpu-priority', choices=['low', 'high'])
    parser.add_argument('--prepend-filters')
    parser.add_argument('--append-filters')
    parser.add_argument('--proto', nargs='?', const=30, type=int, help='Prototype pass (CRF 30-63)')
    parser.add_argument('--print', dest='print_mode', action='store_true', help='Dry-run mode')

    args = parser.parse_args()

    try:
        check_required_commands(REQUIRED_COMMANDS)

        duration, w, h, fps, audio = get_video_info(args.input_file)

        start_sec = get_time_in_seconds(args.start)
        end_sec = get_time_in_seconds(args.end) if args.end else duration
        clip_duration = end_sec - start_sec
        effective_duration = clip_duration / args.speed

        if clip_duration <= 0:
            raise ScriptError("Calculated duration is zero or negative. Check --start and --end.")

        # Bitrate Math
        target_bits = args.size * 8 * 1024 * 1024
        total_bitrate = (target_bits / effective_duration) * 0.95 / 1000
        video_bitrate = max(MIN_VIDEO_BITRATE_KBPS, total_bitrate - (0 if args.mute or not audio else args.audio_bitrate))

        cfg = {
            'start_sec': start_sec, 'clip_duration': clip_duration, 'effective_duration': effective_duration,
            'video_bitrate': video_bitrate, 'src_w': w, 'src_h': h, 'fps': fps,
            'log_prefix': f"passlog_{int(time.time())}"
        }

        # Informational Header
        print(f"--- Compression Plan ---")
        print(f"Input: {args.input_file} ({duration:.2f}s)")
        print(f"Segment: {start_sec:.2f}s -> {end_sec:.2f}s (Raw: {clip_duration:.2f}s)")
        if args.speed != 1.0: print(f"Speed: {args.speed}x (Final: {effective_duration:.2f}s)")
        if args.proto:
            print(f"Mode: PROTOTYPE (CRF: {args.proto})")
        else:
            print(f"Target Size: {args.size} MiB | Video Bitrate: {video_bitrate:.2f} kbps")

        if args.proto:
            run_ffmpeg_pass(1, args, cfg)
        else:
            run_ffmpeg_pass(1, args, cfg)
            run_ffmpeg_pass(2, args, cfg)
            # Clean up pass logs
            for f in [f"{cfg['log_prefix']}-0.log", f"{cfg['log_prefix']}-0.log.temp"]:
                if os.path.exists(f): os.remove(f)

        print("\nProcess finished successfully.")
        sys.exit(0)

    except ScriptError as e:
        print(f"\nFATAL ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nProcess interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\nUNEXPECTED ERROR: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
