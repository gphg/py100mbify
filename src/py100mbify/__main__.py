#!/usr/bin/env python

import argparse
import subprocess
import os
import sys
import shutil
import json
import time
from datetime import datetime, timedelta

# --- Script Configuration ---
REQUIRED_COMMANDS = ['ffprobe', 'ffmpeg']
DEFAULT_TARGET_SIZE_MIB = 100  # Default target output size in MiB
DEFAULT_AUDIO_BITRATE_KBPS = 96 # Default audio bitrate in kbps
MIN_VIDEO_BITRATE_KBPS = 50

class ScriptError(Exception):
    """Custom exception for script errors."""
    pass

def check_required_commands(commands):
    """Check if all required commands are available."""
    for cmd in commands:
        if not shutil.which(cmd):
            raise ScriptError(f"Error: Required command '{cmd}' not found. Please install it.")

def get_video_info(input_file):
    """
    Use ffprobe to get the video's duration, resolution, FPS, and audio stream information.
    Returns duration in seconds, a list of audio streams, video width, video height, and video FPS.
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
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        probe_output = json.loads(result.stdout)

        # Get duration from format section
        duration_seconds = float(probe_output['format']['duration'])

        # Get video stream info
        video_stream = next((s for s in probe_output['streams'] if s['codec_type'] == 'video'), None)
        if not video_stream:
            raise ScriptError("Error: No video stream found in the input file.")

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

def run_ffmpeg_pass(pass_number, input_file, output_file, effective_duration_seconds,
                    target_video_bitrate_kbps, audio_bitrate, mute, speed, start, end,
                    fps, scale, cpu_priority, prepend_filters, append_filters, pass_log_file):
    """Run a single FFmpeg encoding pass."""
    pass_start_time = time.time()
    print(f"\n--- Starting FFmpeg Pass {pass_number} ---")

    # Base command
    cmd = [
        'ffmpeg',
        '-hide_banner',
        '-y'
    ]

    # CPU priority flags
    if cpu_priority == 'low' and os.name == 'posix':
        cmd.insert(0, 'nice')
    elif cpu_priority == 'low' and os.name == 'nt':
        # On Windows, we'll try to set the priority after the process starts
        pass

    # Input file and trim
    if start:
        cmd.extend(['-ss', start])
    if end:
        cmd.extend(['-to', end])

    cmd.extend(['-i', input_file])

    # Video filters list
    video_filters = []

    # Prepend custom filters
    if prepend_filters:
        video_filters.append(prepend_filters)

    # Core filters
    if speed != 1.0:
        video_filters.append(f'setpts={1/speed}*PTS')
    if scale:
        video_filters.append(f'scale=-2:{scale}')
    if fps:
        video_filters.append(f'fps={fps}')

    # Append custom filters
    if append_filters:
        video_filters.append(append_filters)

    # Add video filters to command
    if video_filters:
        cmd.extend(['-vf', ','.join(video_filters)])

    # Video codec and bitrate
    cmd.extend(['-c:v', 'libvpx-vp9', '-b:v', f'{target_video_bitrate_kbps}k'])

    # Audio handling
    if mute:
        cmd.extend(['-an'])
    else:
        cmd.extend(['-c:a', 'libopus', '-b:a', f'{audio_bitrate}k'])

    # Pass-specific options
    if pass_number == 1:
        cmd.extend([
            '-pass', '1',
            '-passlogfile', pass_log_file,
            '-f', 'webm',
            os.devnull
        ])
    elif pass_number == 2:
        cmd.extend([
            '-pass', '2',
            '-passlogfile', pass_log_file,
            '-quality', 'best',
            '-threads', '4', # Use a fixed number of threads for consistent performance
            output_file
        ])

    try:
        if pass_number == 1:
            print("Running FFmpeg pass 1... This may take a moment.")
            subprocess.run(cmd, check=True)
        elif pass_number == 2:
            print("Running FFmpeg pass 2...")
            subprocess.run(cmd, check=True)

    except subprocess.CalledProcessError as e:
        raise ScriptError(f"Error during FFmpeg pass {pass_number}: FFmpeg pass {pass_number} failed. Check the output for details.")

    pass_end_time = time.time()
    duration_seconds = pass_end_time - pass_start_time
    minutes = int(duration_seconds // 60)
    seconds = int(duration_seconds % 60)
    print(f"\n--- FFmpeg Pass {pass_number} completed in {minutes}m {seconds}s ---")

def calculate_bitrates(size, effective_duration_seconds, audio_bitrate, is_audio_enabled):
    """
    Calculates the target total and video bitrates based on target size and duration.
    Returns a tuple (target_total_bitrate_kbps, target_video_bitrate_kbps).
    """
    target_size_bits = size * 8 * 1024 * 1024  # MiB to bits

    if effective_duration_seconds == 0:
        raise ScriptError("Error: Video has a duration of zero. Cannot proceed.")

    # Calculate total bitrate with 5% overhead buffer
    target_total_bitrate_kbps = (target_size_bits / effective_duration_seconds) * 0.95 / 1000

    # Calculate target video bitrate
    audio_bitrate_to_subtract_kbps = audio_bitrate if is_audio_enabled else 0
    target_video_bitrate_kbps = target_total_bitrate_kbps - audio_bitrate_to_subtract_kbps

    # Ensure video bitrate is not too low
    if target_video_bitrate_kbps <= MIN_VIDEO_BITRATE_KBPS:
        target_video_bitrate_kbps = MIN_VIDEO_BITRATE_KBPS

    return target_total_bitrate_kbps, target_video_bitrate_kbps


def compress_video(input_file, output_file=None, size=DEFAULT_TARGET_SIZE_MIB,
                    audio_bitrate=DEFAULT_AUDIO_BITRATE_KBPS, mute=False, speed=1.0,
                    start=None, end=None, fps=None, scale=None, cpu_priority=None,
                    prepend_filters=None, append_filters=None):
    """
    Compresses a video file to a target size using FFmpeg.
    This function contains the core logic for the conversion process.
    """
    try:
        # Check for required commands before doing anything else
        check_required_commands(REQUIRED_COMMANDS)

        # Start a timer for the whole script
        script_start_time = time.time()
        script_start_datetime = datetime.now()

        # Handle optional output file and overwrite check
        if output_file is None:
            base, _ = os.path.splitext(os.path.basename(input_file))
            output_file = f"{base}.webm"

        # Absolute paths for comparison
        abs_input = os.path.abspath(input_file)
        abs_output = os.path.abspath(output_file)
        if abs_input == abs_output:
            raise ScriptError("Error: Input and output file paths are identical. This would overwrite the input file.")

        # --- Video Info and Bitrate Calculation ---
        duration_seconds, audio_streams, video_width, video_height, video_fps = get_video_info(input_file)

        # Apply speed to duration
        effective_duration_seconds = duration_seconds / speed
        is_audio_enabled = not mute and audio_streams

        target_total_bitrate_kbps, target_video_bitrate_kbps = calculate_bitrates(
            size,
            effective_duration_seconds,
            audio_bitrate,
            is_audio_enabled
        )

        # Define pass log file based on output filename
        pass_log_file = os.path.splitext(os.path.basename(output_file))[0] + "_passlog"

        # --- Initial Conversion Summary ---
        print("--- WebM Conversion Script Summary ---")
        print(f"Start Time: {script_start_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Input File: {input_file}")
        print(f"Output File: {output_file}")
        print(f"Target Size: {size} MiB")
        print("--- Video Information ---")
        print(f"Original Resolution: {video_width}x{video_height}")
        print(f"Original FPS: {video_fps:.2f}")
        print(f"Video Duration: {duration_seconds:.2f} seconds")
        if fps:
            print(f"Target FPS: {fps}")
        if scale:
            print(f"Target Scale (smallest dimension): {scale}px")
        print(f"Calculated total target bitrate: {target_total_bitrate_kbps:.2f} kbps")
        print(f"Calculated video bitrate: {target_video_bitrate_kbps:.2f} kbps")
        speed_text = "normal"
        if speed > 1.0:
            speed_text = "faster"
        elif speed < 1.0:
            speed_text = "slower"
        print(f"Target Speed: {speed}x ({speed_text})")
        if start and end:
            print(f"Trimming from {start} to {end}")
        elif start:
            print(f"Trimming from {start} to end of video")
        elif end:
            print(f"Trimming from start of video to {end}")
        print("--- Audio Information ---")
        if not is_audio_enabled:
            print("Audio will be muted.")
        else:
            print(f"Audio Bitrate: {audio_bitrate} kbps")
        print("--- Additional Information ---")
        if cpu_priority:
            print(f"CPU Priority: {cpu_priority}")
        if prepend_filters:
            print(f"Prepending filters: {prepend_filters}")
        if append_filters:
            print(f"Appending filters: {append_filters}")

        print("--------------------------------------")

        # Run FFmpeg pass 1
        run_ffmpeg_pass(1, input_file, os.devnull, effective_duration_seconds, target_video_bitrate_kbps,
                        audio_bitrate, mute, speed, start, end, fps, scale, cpu_priority,
                        prepend_filters, append_filters, pass_log_file)

        # Run FFmpeg pass 2
        run_ffmpeg_pass(2, input_file, output_file, effective_duration_seconds, target_video_bitrate_kbps,
                        audio_bitrate, mute, speed, start, end, fps, scale, cpu_priority,
                        prepend_filters, append_filters, pass_log_file)

        # Get final output file size
        final_size_bytes = os.path.getsize(output_file)
        final_size_mib = final_size_bytes / (1024 * 1024)

        # Cleanup
        os.remove(f'{pass_log_file}-0.log')
        if os.path.exists(f'{pass_log_file}-0.log.temp'):
            os.remove(f'{pass_log_file}-0.log.temp')

        # Add a print statement for the total time taken
        script_end_time = time.time()
        script_end_datetime = datetime.now()
        total_time = script_end_time - script_start_time

        # Final report after conversion
        print(f"\nCompression completed successfully!")
        print(f"Final Output Size: {final_size_mib:.2f} MiB")
        print(f"Total Time Taken: {str(timedelta(seconds=total_time)).split('.')[0]}")
        print(f"End Time: {script_end_datetime.strftime('%Y-%m-%d %H:%M:%S')}")

    except ScriptError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    """Parses command-line arguments and calls the compression function."""
    # --- Argument Parsing ---
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
    parser.add_argument('--cpu-priority', choices=['low', 'high'],
                        help='(Optional) Set FFmpeg process CPU priority to low or high.')
    parser.add_argument('--prepend-filters', help='(Optional) FFmpeg filters to apply before standard filters.')
    parser.add_argument('--append-filters', help='(Optional) FFmpeg filters to apply after standard filters.')
    args = parser.parse_args()

    # Pass parsed arguments to the core compression function
    compress_video(**vars(args))


if __name__ == '__main__':
    main()
