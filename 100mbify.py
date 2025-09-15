#!/usr/bin/env python
import argparse
import subprocess
import os
import re
import sys
import shutil
import json
import time

# --- Script Configuration ---
REQUIRED_COMMANDS = ['ffprobe', 'ffmpeg']
DEFAULT_TARGET_SIZE_MIB = 100  # Default target output size in MiB
DEFAULT_AUDIO_BITRATE_KBPS = 96 # Default audio bitrate in kbps

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

        video_width = video_stream.get('width')
        video_height = video_stream.get('height')
        # Use average frame rate if available, otherwise fallback to standard frame rate
        video_fps_str = video_stream.get('avg_frame_rate', '0/1')
        try:
            num, den = map(int, video_fps_str.split('/'))
            video_fps = num / den if den != 0 else 0
        except (ValueError, IndexError):
            video_fps = 0

        # Get audio streams
        audio_streams = [s for s in probe_output['streams'] if s['codec_type'] == 'audio']

        return duration_seconds, audio_streams, video_width, video_height, video_fps

    except FileNotFoundError:
        raise ScriptError("Error: `ffprobe` not found. Please ensure it's in your PATH.")
    except subprocess.CalledProcessError as e:
        raise ScriptError(f"Error: ffprobe failed for '{input_file}': {e.stderr}")
    except (json.JSONDecodeError, KeyError) as e:
        raise ScriptError(f"Error: Could not parse ffprobe output: {e}")

def run_ffmpeg_pass(pass_number, input_file, output_file, duration, args, pass_log_file):
    """
    Construct and run a single FFmpeg pass.
    Includes simple progress monitoring.
    """
    print(f"\n--- Starting FFmpeg Pass {pass_number} ---")
    
    # Base command
    ffmpeg_cmd = [
        'ffmpeg',
        '-hide_banner', '-v', 'warning',
    ]

    # Set CPU priority if specified
    creation_flags = 0
    if args.cpu_priority == 'low':
        if sys.platform == 'win32':
            creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.IDLE_PRIORITY_CLASS
        else:
            ffmpeg_cmd = ['nice', '-n', '19'] + ffmpeg_cmd
    elif args.cpu_priority == 'high':
        if sys.platform == 'win32':
            creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.HIGH_PRIORITY_CLASS
        else:
            ffmpeg_cmd = ['nice', '-n', '-20'] + ffmpeg_cmd

    # Time trimming arguments
    if args.start:
        ffmpeg_cmd.extend(['-ss', str(args.start)])
    if args.end:
        ffmpeg_cmd.extend(['-to', str(args.end)])

    ffmpeg_cmd.extend([
        '-i', input_file
    ])
    
    # Set up video filters in an array, ordered by type
    video_filters = []
    
    # 1. Selection Filters
    if args.speed != 1.0:
        video_filters.append(f'setpts={1.0 / args.speed}*PTS')
        
    # 2. Conversion Filters
    if args.scale:
        video_filters.append(f"scale='if(gt(iw,ih),-2,{args.scale})':'if(gt(iw,ih),{args.scale},-2)'")
    if args.fps:
        video_filters.append(f'fps={args.fps}')
        
    # Add video filters to the command if any are present
    if video_filters:
        ffmpeg_cmd.extend(['-vf', ','.join(video_filters)])
        
    # Bitrate and codec arguments
    ffmpeg_cmd.extend([
        '-c:v', 'libvpx-vp9',
        '-g', '240',
        '-quality', 'best',
        '-b:v', f'{args.target_video_bitrate_kbps}k',
        '-pass', str(pass_number),
        '-passlogfile', pass_log_file,
    ])
    
    # Audio arguments for pass 2
    if pass_number == 2:
        if args.mute:
            ffmpeg_cmd.append('-an')
        else:
            # Reapply atempo filter for audio if speed is changed
            if args.speed != 1.0:
                if args.speed > 2.0:
                    current_speed = args.speed
                    atempo_filters = []
                    while current_speed > 2.0:
                        atempo_filters.append('atempo=2.0')
                        current_speed /= 2.0
                    atempo_filters.append(f'atempo={current_speed}')
                    atempo_filter = ','.join(atempo_filters)
                elif args.speed < 0.5:
                    current_speed = args.speed
                    atempo_filters = []
                    while current_speed < 0.5:
                        atempo_filters.append('atempo=0.5')
                        current_speed /= 0.5
                    atempo_filters.append(f'atempo={current_speed}')
                    atempo_filter = ','.join(atempo_filters)
                else:
                    atempo_filter = f'atempo={args.speed}'
                ffmpeg_cmd.extend(['-af', atempo_filter])
            
            ffmpeg_cmd.extend([
                '-c:a', 'libopus',
                '-b:a', f'{args.audio_bitrate}k'
            ])
            
    # Overwrite output
    ffmpeg_cmd.append('-y')

    # FIX: Add '-f webm' for the first pass to explicitly define the format.
    if pass_number == 1:
        ffmpeg_cmd.extend(['-f', 'webm'])

    # Output file
    ffmpeg_cmd.append(output_file)

    try:
        start_time = time.time()
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creation_flags
        )
        
        stdout_output, stderr_output = process.communicate()
        end_time = time.time()
        duration_seconds = end_time - start_time
        
        if process.returncode != 0:
            print(stderr_output)
            raise ScriptError(f"Error: FFmpeg pass {pass_number} failed. Check the output for details.")
            
        print(f"\n--- FFmpeg Pass {pass_number} completed in {duration_seconds:.2f} seconds ---")

    except FileNotFoundError:
        raise ScriptError("Error: `ffmpeg` not found. Please ensure it's in your PATH.")
    except Exception as e:
        raise ScriptError(f"Error during FFmpeg pass {pass_number}: {e}")

def main():
    """Main function to parse arguments and run the conversion process."""
    # Add a variable to store the start time of the entire script
    script_start_time = time.time()

    parser = argparse.ArgumentParser(description='Compress a video file to a target size using FFmpeg VP9 two-pass encoding.')
    parser.add_argument('input_file', help='Path to the input video file.')
    parser.add_argument('output_file', nargs='?', help='Optional: Path for the output video file. If omitted, will be generated.')
    parser.add_argument('--size', type=int, default=DEFAULT_TARGET_SIZE_MIB,
                        help=f'Target output size in MiB. (default: {DEFAULT_TARGET_SIZE_MIB})')
    parser.add_argument('--audio-bitrate', type=int, default=DEFAULT_AUDIO_BITRATE_KBPS,
                        help=f'Target audio bitrate in kbps. (default: {DEFAULT_AUDIO_BITRATE_KBPS})')
    parser.add_argument('--start', help='Start time for trimming (e.g., "00:01:30" or "90").')
    parser.add_argument('--end', help='End time for trimming (e.g., "00:02:00" or "120").')
    parser.add_argument('--fps', type=int, help='Target frames per second for the output video.')
    parser.add_argument('--speed', type=float, default=1.0,
                        help='Speed multiplier for the video. (e.g., 2.0 for 2x speed).')
    parser.add_argument('--mute', action='store_true', help='Mute the audio in the output video.')
    parser.add_argument('--scale', type=int, help="The target size for the video's smallest dimension (e.g., '720' for 720p equivalent).")
    parser.add_argument('--cpu-priority', choices=['high', 'low'], help='Set the CPU priority for the FFmpeg process. (e.g., "high" or "low")')

    args = parser.parse_args()

    # Pre-flight checks
    try:
        check_required_commands(REQUIRED_COMMANDS)
    except ScriptError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    # If no output file is provided, generate one
    if not args.output_file:
        input_filename_base, _ = os.path.splitext(os.path.basename(args.input_file))
        args.output_file = f"{input_filename_base}.webm"

    # Check for accidental overwrite
    if os.path.abspath(args.input_file) == os.path.abspath(args.output_file):
        print(f"Error: Output file path '{args.output_file}' is the same as the input file. Aborting to prevent overwrite.", file=sys.stderr)
        sys.exit(1)

    # Convert MiB to bits
    target_size_bits = args.size * 1024 * 1024 * 8
    
    try:
        # Get video duration from ffprobe
        print(f"Probing '{args.input_file}' for duration...")
        duration_seconds, audio_streams, video_width, video_height, video_fps = get_video_info(args.input_file)
        
        if duration_seconds <= 0:
            raise ScriptError("Error: Video has a duration of zero. Cannot proceed.")
            
        # Calculate target bitrates
        is_audio_enabled = not args.mute and bool(audio_streams)
        
        if not is_audio_enabled:
            total_bitrate = target_size_bits / duration_seconds
            args.target_video_bitrate_kbps = total_bitrate / 1000
        else:
            audio_bitrate_bits_per_sec = args.audio_bitrate * 1000
            video_bitrate_bits_per_sec = (target_size_bits - (audio_bitrate_bits_per_sec * duration_seconds)) / duration_seconds
            args.target_video_bitrate_kbps = video_bitrate_bits_per_sec / 1000
            
        # Define pass log file based on output filename to prevent conflicts
        base_name = os.path.splitext(os.path.basename(args.output_file))[0]
        pass_log_file = f"{base_name}_passlog"
        args.pass_log_file = pass_log_file

        # Display a summary of the conversion settings before starting
        print("--- WebM Conversion Script Summary ---")
        print(f"Input File: {args.input_file}")
        print(f"Output File: {args.output_file}")
        print(f"Target Size: {args.size} MiB")
        print("--- Video Information ---")
        print(f"Original Resolution: {video_width}x{video_height}")
        print(f"Original FPS: {video_fps:.2f}")
        print(f"Video Duration: {duration_seconds:.2f} seconds")
        if args.fps:
            print(f"Target FPS: {args.fps}")
        if args.scale:
            print(f"Target Scale (smallest dimension): {args.scale}px")
        print(f"Calculated target video bitrate: {args.target_video_bitrate_kbps:.2f} kbps")
        speed_text = "normal"
        if args.speed > 1.0:
            speed_text = "faster"
        elif args.speed < 1.0:
            speed_text = "slower"
        print(f"Target Speed: {args.speed}x ({speed_text})")
        if args.start and args.end:
            print(f"Trimming from {args.start} to {args.end}")
        elif args.start:
            print(f"Trimming from {args.start} to end of video")
        elif args.end:
            print(f"Trimming from start of video to {args.end}")
        print("--- Audio Information ---")
        if not is_audio_enabled:
            print("Audio will be muted.")
        else:
            print(f"Audio Bitrate: {args.audio_bitrate} kbps")
        
        if args.cpu_priority:
            print(f"CPU Priority: {args.cpu_priority}")
            
        print("--------------------------------------")
        
        # Run FFmpeg pass 1
        run_ffmpeg_pass(1, args.input_file, os.devnull, duration_seconds, args, pass_log_file)
        
        # Run FFmpeg pass 2
        run_ffmpeg_pass(2, args.input_file, args.output_file, duration_seconds, args, pass_log_file)
        
        # Get final output file size
        final_size_bytes = os.path.getsize(args.output_file)
        final_size_mib = final_size_bytes / (1024 * 1024)

        # Cleanup
        os.remove(f'{pass_log_file}-0.log')
        if os.path.exists(f'{pass_log_file}-0.log.temp'):
            os.remove(f'{pass_log_file}-0.log.temp')
            
        # Add a print statement for the total time taken
        script_end_time = time.time()
        total_time = script_end_time - script_start_time

        # Final report after conversion
        print(f"\nCompression completed successfully!")
        print(f"Final Output Size: {final_size_mib:.2f} MiB")
        print(f"Total time taken: {total_time:.2f} seconds.")
        
    except ScriptError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
