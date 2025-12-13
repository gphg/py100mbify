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
            # Fallback to float conversion if parsing failed
            # This is primarily for safety, FFmpeg parsing should catch timecodes
            return 0.0

def escape_ffmpeg_path(path):
    """
    Escapes a file path for use in an FFmpeg filter string.
    Crucial for Windows paths with backslashes and drive colons.
    """
    # Convert backslashes to forward slashes
    path = path.replace('\\', '/')
    # Escape colons (e.g., C:/ becomes C\:/)
    path = path.replace(':', '\\:')
    # Handle single quotes if present (rare but possible)
    path = path.replace("'", "'\\\\''")
    return path

def get_video_info(input_file):
    """
    Use ffprobe to get the video's duration, resolution, FPS, and audio stream information.
    Returns duration in seconds, a list of audio streams, video width, video height, and video FPS.

    Includes the critical encoding fix for Windows environments.
    """
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
            encoding='utf-8', # Use UTF-8 for reading output
            errors='replace' # Handle potential encoding errors gracefully
        )
        probe_output = json.loads(result.stdout)

        # Get duration from format section
        duration_seconds = float(probe_output['format']['duration'])

        # Get video stream info
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

        # Get audio streams
        audio_streams = [s for s in probe_output['streams'] if s['codec_type'] == 'audio']

        return duration_seconds, audio_streams, video_width, video_height, video_fps

    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
        raise ScriptError(f"Error: ffprobe failed to get video information. Details: {e}")

def run_ffmpeg_pass(pass_number, input_file, output_file, effective_duration_seconds, clip_duration_seconds,
                    target_video_bitrate_kbps, audio_bitrate, mute, speed, start, end,
                    fps, scale, cpu_priority, prepend_filters, append_filters, pass_log_file,
                    threads, quality, rotate, keep_metadata, hard_sub=False, target_web=False, proto=False):
    """
    Run a single FFmpeg encoding pass using subprocess.Popen to allow
    FFmpeg's progress output to stream directly to the console.
    """
    if proto and pass_number == 1:
        # In PROTO mode, we skip the first pass
        return

    pass_start_time = time.time()

    if proto:
        print(f"\n--- Starting FFmpeg Prototype Pass (CRF) ---")
    else:
        print(f"\n--- Starting FFmpeg Pass {pass_number} ---")

    # Base command
    cmd = [
        'ffmpeg',
        '-hide_banner',
        # Skip interuption and also overwrite on existing files!
        '-y',
         # Prevents accidental keyboard input from stopping the process
        '-nostdin',
    ]

    # Input file and trim: -ss BEFORE -i for fast seek on the source file.
    if start:
        cmd.extend(['-ss', start])

    cmd.extend(['-i', input_file])

    # Use -t (duration) instead of -to (end time) for accurate clipping when combined with -ss before -i.
    if clip_duration_seconds:
        cmd.extend(['-t', f'{clip_duration_seconds:.3f}'])

    # Video filters list
    video_filters = []

    # Prepend custom filters
    if prepend_filters:
        video_filters.append(prepend_filters)

    # --- Core filters ---

    # 1. Hardsub (MUST BE FIRST to handle timestamp sync relative to source file)
    if hard_sub:
        start_seconds = get_time_in_seconds(start)
        escaped_input = escape_ffmpeg_path(input_file)

        # We need to burn subtitles. However, because we used Fast Seek (-ss before -i),
        # the video timestamps start at 0, but the subtitle file expects timestamps
        # matching the original video time (e.g. 15mins in).
        # We use setpts to temporarily shift timestamps forward, apply subs, then shift back.

        # Step A: Shift timestamps forward to match original source time
        video_filters.append(f"setpts=PTS+({start_seconds}/TB)")

        # Step B: Apply subtitles from the input file
        video_filters.append(f"subtitles='{escaped_input}'")

        # Step C: Shift timestamps back to zero (relative to clip start) for encoding
        video_filters.append("setpts=PTS-STARTPTS")

        # Step D: Clear the soft subtitle, as it is not necessarily anymore
        cmd.extend(['-sn'])

    # 2. Rotation
    if rotate is not None:
        rotation_radians = math.radians(rotate)
        video_filters.append(f'rotate={rotation_radians}')

    # 3. Speed (SET PTS) - Applied after hardsub so subs aren't time-warped weirdly before rendering
    if speed != 1.0:
        # Applies speed filter before scaling/cropping/etc.
        video_filters.append(f'setpts={1/speed}*PTS')

    # 4. Scale
    if scale:
        video_filters.append(f'scale=-2:{scale}')

    # 5. FPS
    if fps:
        video_filters.append(f'fps={fps}')

    # Append custom filters
    if append_filters:
        video_filters.append(append_filters)

    # Add video filters to command
    if video_filters:
        cmd.extend(['-vf', ','.join(video_filters)])

    # Video codec and bitrate/quality
    cmd.extend(['-c:v', 'libvpx-vp9'])

    # --- Web Compatibility Flags ---
    if target_web:
        # Forces 8-bit color depth (yuv420p) and VP9 Profile 0
        cmd.extend(['-pix_fmt', 'yuv420p', '-profile:v', '0'])

    # --- FFmpeg Command Options based on mode/pass ---
    if proto:
        # PROTO Mode: 1-pass CRF for speed, skip Pass 1 entirely.
        print("Using Prototype Mode: Single-pass CRF 30 with realtime deadline.")
        cmd.extend([
            '-crf', '48',
            '-b:v', '0',
            '-quality', 'realtime',
            '-deadline', 'realtime',
            '-threads', str(threads)
        ])
    else:
        # Full 2-pass target size compression
        cmd.extend(['-b:v', f'{target_video_bitrate_kbps}k'])

        if pass_number == 1:
            cmd.extend([
                '-pass', '1',
                '-passlogfile', pass_log_file,
                '-f', 'webm',
                os.devnull
            ])
        elif pass_number == 2:
            if keep_metadata:
                cmd.extend(['-map_metadata', '0'])
            cmd.extend([
                '-pass', '2',
                '-passlogfile', pass_log_file,
                '-quality', quality,
                '-threads', str(threads)
            ])

    # Audio handling (Applies to both PROTO and 2-pass)
    if mute:
        cmd.extend(['-an'])
    else:
        # Only add audio for non-pass 1 of 2-pass or for PROTO
        if not (not proto and pass_number == 1):
             # Ensure audio bitrate is set, especially for proto mode to avoid the default 96k warning
             cmd.extend(['-c:a', 'libopus', '-b:a', f'{audio_bitrate}k'])

    # Final output file (Applies to Pass 2 and PROTO)
    if pass_number == 2 or proto:
        cmd.append(output_file)

    # --- End FFmpeg Command Options ---

    # Optional: Set process CPU priority (if available and requested)
    if cpu_priority == 'low':
        # On Linux/macOS, use 'nice'. On Windows, this is often ignored or handled by the shell.
        # We rely on FFmpeg's internal process priority handling for simplicity here,
        # but in a production script, platform-specific code would be needed.
        pass

    process = None
    print(f"\nExecuting command: {' '.join(cmd)}")
    print(f"Running FFmpeg...")

    try:
        # Use Popen to let FFmpeg write directly to the console's stderr for real-time updates.
        # We only capture output if it's Pass 1, as the output is discarded anyway.
        # Otherwise, we use PIPE/DEVNULL to ensure the process starts correctly,
        # but rely on the environment's ability to stream the progress.
        process = subprocess.Popen(
            cmd,
            # FFmpeg writes progress to stderr, so we usually want to let stderr go to the console.
            # stdout=subprocess.DEVNULL,
            # stderr=subprocess.STDOUT, # Directing stderr to stdout sometimes works better for streaming
            # The current working solution relies on neither being captured.
        )

        # Wait for the process to finish
        process.wait()

        return_code = process.returncode

        if return_code != 0:
             raise ScriptError(f"Error during FFmpeg pass {pass_number}: FFmpeg failed with exit code {return_code}. Check the log output above for details.")

    except KeyboardInterrupt:
        # This block allows Ctrl+C to stop the encoding process gracefully
        print("\nInterrupt received. Terminating FFmpeg process...")
        if process and process.poll() is None:
            process.terminate()
            # Wait for a brief moment for it to terminate
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # If it's still alive, force kill
                print("FFmpeg did not terminate. Forcing kill...")
                process.kill()
        sys.exit(1) # Exit the script upon interruption
    except Exception as e:
        # Other potential errors
        if process and process.poll() is None:
            process.kill() # Ensure process is killed on other errors
        raise ScriptError(f"An unexpected error occurred during FFmpeg pass {pass_number}: {e}")

    pass_end_time = time.time()
    duration_seconds = pass_end_time - pass_start_time
    minutes = int(duration_seconds // 60)
    seconds = int(duration_seconds % 60)

    if proto:
        print(f"\n--- FFmpeg Prototype Pass completed in {minutes}m {seconds}s ---")
    else:
        print(f"\n--- FFmpeg Pass {pass_number} completed in {minutes}m {seconds}s ---")


def calculate_bitrates(size, effective_duration_seconds, audio_bitrate, is_audio_enabled):
    """
    Calculates the target total and video bitrates based on target size and DURATION OF THE OUTPUT FILE.
    Returns a tuple (target_total_bitrate_kbps, target_video_bitrate_kbps).
    """
    if effective_duration_seconds <= 0:
         raise ScriptError("Error: Effective duration for bitrate calculation is zero or negative.")

    target_size_bits = size * 8 * 1024 * 1024  # MiB to bits

    # Calculate total bitrate with 5% overhead buffer
    # Use 0.95 factor to leave a 5% buffer for muxing overhead
    target_total_bitrate_kbps = (target_size_bits / effective_duration_seconds) * 0.95 / 1000

    # Calculate target video bitrate
    audio_bitrate_to_subtract_kbps = audio_bitrate if is_audio_enabled else 0
    target_video_bitrate_kbps = target_total_bitrate_kbps - audio_bitrate_to_subtract_kbps

    # Ensure video bitrate is not too low
    if target_video_bitrate_kbps <= MIN_VIDEO_BITRATE_KBPS:
        print(f"Warning: Calculated video bitrate ({target_video_bitrate_kbps:.2f} kbps) is too low. Setting minimum to {MIN_VIDEO_BITRATE_KBPS} kbps.")
        target_video_bitrate_kbps = MIN_VIDEO_BITRATE_KBPS

    return target_total_bitrate_kbps, target_video_bitrate_kbps


def compress_video(input_file, output_file=None, size=DEFAULT_TARGET_SIZE_MIB,
                    audio_bitrate=DEFAULT_AUDIO_BITRATE_KBPS, mute=False, speed=1.0,
                    start=None, end=None, fps=None, scale=None, cpu_priority=None,
                    prepend_filters=None, append_filters=None, rotate=None, keep_metadata=False,
                    hard_sub=False, target_web=False, info_detail=False, proto=False):
    """
    Compresses a video file to a target size using FFmpeg.
    """
    try:
        # Check for required commands before doing anything else
        check_required_commands(REQUIRED_COMMANDS)

        # Start a timer for the whole script
        script_start_time = time.time()
        script_start_datetime = datetime.now()

        # Handle optional output file
        if output_file is None:
            base, _ = os.path.splitext(os.path.basename(input_file))
            output_file = f"{base}.webm"

        # Read configurable FFmpeg options from environment variables with fallbacks
        threads = int(os.environ.get('PY100MBIFY_THREADS', DEFAULT_THREADS))
        quality = os.environ.get('PY100MBIFY_QUALITY', DEFAULT_QUALITY)

        # --- Video Info and Duration Calculation ---
        duration_seconds, audio_streams, video_width, video_height, video_fps = get_video_info(input_file)

        # 1. Calculate the PHYSICAL duration of the clip being processed (takes trimming into account)
        clip_duration_seconds = duration_seconds

        start_sec = get_time_in_seconds(start) if start else 0.0

        if end:
            end_sec = get_time_in_seconds(end)

            if start:
                clip_duration_seconds = end_sec - start_sec
            else: # Only End defined (-to after -i)
                clip_duration_seconds = end_sec
        elif start: # Only Start defined (-ss before -i)
             clip_duration_seconds = duration_seconds - start_sec

        # Ensure we have a valid duration to encode
        if clip_duration_seconds <= 0:
            raise ScriptError("Error: Calculated clip duration is zero or negative. Check --start and --end parameters.")

        # 2. Apply speed factor to get the FINAL duration of the output file
        effective_duration_seconds = clip_duration_seconds / speed
        is_audio_enabled = not mute and audio_streams

        target_total_bitrate_kbps, target_video_bitrate_kbps = calculate_bitrates(
            size,
            effective_duration_seconds,
            audio_bitrate,
            is_audio_enabled
        )

        # Define pass log file
        log_base_name = os.path.splitext(os.path.basename(output_file))[0]
        pass_log_file = os.path.join(os.path.dirname(output_file) or os.getcwd(), f"{log_base_name}_passlog")

        # --- Initial Conversion Summary ---
        if info_detail:
            print("--- WebM Conversion Script Summary ---")
            print(f"Start Time: {script_start_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"Input File: {input_file}")
            print(f"Output File: {output_file}")
            print(f"Mode: {'Prototype (Fast Clip Check - CRF 30)' if proto else 'Target Size (2-Pass VBR)'}")
            if not proto:
                print(f"Target Size: {size} MiB")
            print("--- Video Information ---")
            print(f"Original Resolution: {video_width}x{video_height}")
            print(f"Original FPS: {video_fps:.2f}")
            print(f"Input Video Duration: {duration_seconds:.2f} seconds")

            # Print Trimming/Duration details clearly
            if start or end:
                print(f"Trimming Start: {start if start else '0.0'} (s)")
                print(f"Trimming End: {end if end else 'End'} (s)")

            print(f"Encoded Clip Duration (Content Length): {clip_duration_seconds:.2f} seconds")

            if speed != 1.0:
                print(f"Playback Speed: {speed}x")

            print(f"Final Output Duration (Time-Scaled): {effective_duration_seconds:.2f} seconds")

            if scale:
                 print(f"Target Scale (min dimension): {scale}p")
            if fps:
                 print(f"Target FPS: {fps}")
            if rotate is not None:
                print(f"Rotation: {rotate} degrees")
            if hard_sub:
                print(f"Hardsub: Enabled (Burning subtitles from {os.path.basename(input_file)})")

            # Print Web Compatibility Status
            if target_web:
                print("Web Compatibility: Enabled (Forcing 8-bit yuv420p, Profile 0)")

            print("--- Audio Information ---")
            if not is_audio_enabled:
                print("Audio will be **muted**.")
            else:
                print(f"Audio Bitrate: {audio_bitrate} kbps (Enabled)")

            if not proto:
                print("--- Calculated Bitrates ---")
                print(f"Target Total Bitrate: {target_total_bitrate_kbps:.2f} kbps")
                print(f"Target Video Bitrate: {target_video_bitrate_kbps:.2f} kbps")

            print("--- Additional Configuration ---")
            print(f"FFmpeg Threads: {threads}")
            if not proto:
                print(f"VP9 Quality Setting: {quality}")
            if cpu_priority:
                print(f"CPU Priority: {cpu_priority}")
            if prepend_filters:
                print(f"Prepending filters: {prepend_filters}")
            if append_filters:
                print(f"Appending filters: {append_filters}")
            print("--------------------------------------")


        # Run FFmpeg pass(es)
        if not proto:
            # Pass 1 (only for 2-pass mode)
            run_ffmpeg_pass(1, input_file, os.devnull, effective_duration_seconds, clip_duration_seconds, target_video_bitrate_kbps,
                            audio_bitrate, mute, speed, start, end, fps, scale, cpu_priority,
                            prepend_filters, append_filters, pass_log_file, threads, quality, rotate, keep_metadata,
                            hard_sub=hard_sub, target_web=target_web, proto=proto)

            # Pass 2
            run_ffmpeg_pass(2, input_file, output_file, effective_duration_seconds, clip_duration_seconds, target_video_bitrate_kbps,
                            audio_bitrate, mute, speed, start, end, fps, scale, cpu_priority,
                            prepend_filters, append_filters, pass_log_file, threads, quality, rotate, keep_metadata,
                            hard_sub=hard_sub, target_web=target_web, proto=proto)
        else:
            # PROTO mode (single-pass)
            run_ffmpeg_pass(2, input_file, output_file, effective_duration_seconds, clip_duration_seconds, target_video_bitrate_kbps,
                            audio_bitrate, mute, speed, start, end, fps, scale, cpu_priority,
                            prepend_filters, append_filters, pass_log_file, threads, quality, rotate, keep_metadata,
                            hard_sub=hard_sub, target_web=target_web, proto=proto)

        # Get final output file size
        final_size_bytes = os.path.getsize(output_file)
        final_size_mib = final_size_bytes / (1024 * 1024)

        # Cleanup pass log files (only relevant for 2-pass mode)
        if not proto:
            # Find and remove log files created by FFmpeg's -passlogfile
            # The file names are typically <pass_log_file>-0.log and <pass_log_file>-0.log.temp
            log_path = f'{pass_log_file}-0.log'
            temp_log_path = f'{pass_log_file}-0.log.temp'

            for log_file in [log_path, temp_log_path]:
                try:
                    if os.path.exists(log_file):
                        os.remove(log_file)
                except OSError as e:
                    print(f"Warning: Failed to remove temporary log file {log_file}. {e}", file=sys.stderr)

        # Final report after conversion
        script_end_time = time.time()
        script_end_datetime = datetime.now()
        total_time = script_end_time - script_start_time

        print(f"\nCompression completed successfully!")
        print(f"Output: {output_file}")
        print(f"Final Output Size: {final_size_mib:.2f} MiB")
        if info_detail:
            print(f"Total Time Taken: {str(timedelta(seconds=total_time)).split('.')[0]}")
            print(f"End Time: {script_end_datetime.strftime('%Y-%m-%d %H:%M:%S')}")

        return output_file, final_size_mib

    except ScriptError as e:
        print(f"Error in compressing {input_file} segment: {e}", file=sys.stderr)
        return None, None
    except Exception as e:
        # Catch unexpected errors and ensure a clean exit
        print(f"An unexpected error occurred during compression: {e}", file=sys.stderr)
        return None, None

# --- Main CLI Functionality (only runs when script is executed directly) ---
def main():
    """Parses command-line arguments and calls the compression function."""
    parser = argparse.ArgumentParser(description='Compresses a video file to a target size using FFmpeg.')
    parser.add_argument('input_file', help='Path to the input video file.')
    parser.add_argument('output_file', nargs='?', help='(Optional) Desired path for the output WebM video file. If omitted, saves as original input video filename with .webm extension.')
    parser.add_argument('--size', type=int, default=DEFAULT_TARGET_SIZE_MIB,
                        help=f'Target output size in MiB. (default: {DEFAULT_TARGET_SIZE_MIB})')
    parser.add_argument('--audio-bitrate', type=int, default=DEFAULT_AUDIO_BITRATE_KBPS,
                        help=f'Target audio bitrate in kbps. (default: {DEFAULT_AUDIO_BITRATE_KBPS})')
    parser.add_argument('--mute', action='store_true', help='Mute the audio track.')
    parser.add_argument('--speed', type=float, default=1.0,
                        help='Video playback speed. (e.g., 0.5 for half speed, 2.0 for double speed).')
    parser.add_argument('--start', help='(Optional) Start time for trimming (e.g., 00:01:30 or 90).')
    parser.add_argument('--end', help='(Optional) End time for trimming (e.g., 00:02:00 or 120).')
    parser.add_argument('--fps', type=int, help='(Optional) Set a target frame rate (e.g., 30).')
    parser.add_argument('--scale', type=int,
                        help='(Optional) The target size for the video\'s smallest dimension (e.g., 720 for 720p equivalent). The other dimension will be calculated to maintain aspect ratio.')
    parser.add_argument('--rotate', type=float,
                        help='(Optional) Rotate the video by the specified number of degrees. Positive values rotate clockwise, negative values rotate counter-clockwise (to the left).')
    parser.add_argument('--keep-metadata', action='store_true',
                        help='(Optional) Keep all original metadata from the input file.')
    parser.add_argument('--hard-sub', action='store_true',
                        help='(Optional) Burn subtitles from the input file into the video. Handles sync automatically when trimming.')
    parser.add_argument('--target-web', action='store_true',
                        help='(Optional) Force 8-bit color depth (yuv420p) and VP9 Profile 0 for better web browser compatibility.')
    parser.add_argument('--cpu-priority', choices=['low', 'high'],
                        help='(Optional) Set FFmpeg process CPU priority to low or high.')
    parser.add_argument('--prepend-filters', help='(Optional) FFmpeg filters to apply before standard filters.')
    parser.add_argument('--append-filters', help='(Optional) FFmpeg filters to apply after standard filters.')
    parser.add_argument('--proto', action='store_true',
                        help='(Optional) Prototype mode: Use fast, low-quality single-pass CRF encoding to quickly test clipping accuracy.') # Added proto
    args = parser.parse_args()

    # Pass parsed arguments to the core compression function, ensuring info_detail is TRUE for CLI runs
    compress_video(info_detail=True, **vars(args))


if __name__ == '__main__':
    main()
