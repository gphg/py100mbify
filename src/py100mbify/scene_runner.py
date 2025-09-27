#!/usr/bin/env python

import argparse
import csv
import os
import sys
from pathlib import Path
from py100mbify.__init__ import compress_video, ScriptError, DEFAULT_TARGET_SIZE_MIB, DEFAULT_AUDIO_BITRATE_KBPS, get_video_info, get_time_in_seconds

# Helper function to parse arguments that are specific to the runner script
def parse_runner_args(argv=None):
    """
    Parses arguments for the scene runner, leaving remaining arguments for compression.
    """
    # Create a dummy parser to handle only the scene_runner arguments
    runner_parser = argparse.ArgumentParser(add_help=False)
    runner_parser.add_argument('input_video', type=Path, help='The path to the original input video file.')
    runner_parser.add_argument('scenes_csv', type=Path, help='The path to the SceneDetect CSV file.')
    runner_parser.add_argument('--output-dir', type=Path, default=Path('./out_scenes'),
                               help='Directory where compressed scene files will be saved (default: ./out_scenes).')

    # Parse only known arguments for the runner
    runner_args, remaining_args = runner_parser.parse_known_args(argv)

    # Now create the final parser for the compression arguments, reusing the runner_parser arguments
    compress_parser = argparse.ArgumentParser(
        parents=[runner_parser],
        description='Compresses multiple scenes from a video based on a SceneDetect CSV.',
        epilog='All arguments not listed above (like --size, --scale, --rotate, etc.) are passed directly to compress_video for each scene.'
    )

    # We re-add the most common py100mbify arguments for proper help text
    compress_parser.add_argument('--size', type=int, default=DEFAULT_TARGET_SIZE_MIB,
                                 help=f'Target output size in MiB for *each* scene (default: {DEFAULT_TARGET_SIZE_MIB}).')
    compress_parser.add_argument('--audio-bitrate', type=int, default=DEFAULT_AUDIO_BITRATE_KBPS,
                                 help=f'Target audio bitrate in kbps (default: {DEFAULT_AUDIO_BITRATE_KBPS}).')
    compress_parser.add_argument('--mute', action='store_true', help='Mute the audio track for all scenes.')
    compress_parser.add_argument('--speed', type=float, default=1.0, help='Video playback speed for all scenes.')
    compress_parser.add_argument('--scale', type=int, default=None, help='The target size for the video\'s smallest dimension.')
    compress_parser.add_argument('--rotate', type=float, default=None, help='Rotate the video by degrees.')
    compress_parser.add_argument('--keep-metadata', action='store_true', help='Keep original metadata.')
    compress_parser.add_argument('--cpu-priority', choices=['low', 'high'], help='Set FFmpeg process CPU priority.')

    # Final comprehensive parse to ensure all arguments are captured in one object
    final_args = compress_parser.parse_args(argv)
    return final_args

def run_scene_compression():
    """
    Reads CSV, prepares arguments for each scene, and calls the compress_video function.
    """
    try:
        args = parse_runner_args()
    except SystemExit:
        return

    input_file = args.input_video
    csv_file = args.scenes_csv
    output_dir = args.output_dir

    if not input_file.exists() or not csv_file.exists():
        sys.stderr.write(f"Error: Input file or CSV not found. Video: {input_file}, CSV: {csv_file}\n")
        sys.exit(1)

    try:
        with open(csv_file, 'r', newline='') as f:
            reader = csv.DictReader(f)
            scenes_data = list(reader)
    except Exception as e:
        sys.stderr.write(f"Error reading or parsing CSV file: {e}\n")
        sys.exit(1)

    if not scenes_data:
        sys.stderr.write("No scenes found in the CSV file.\n")
        return

    # Create output directory
    output_dir.mkdir(exist_ok=True)

    # Extract common compression arguments to pass to compress_video
    # We use vars() to get a dictionary of all arguments, then filter out runner-specific ones
    compression_kwargs = vars(args).copy()
    del compression_kwargs['input_video']
    del compression_kwargs['scenes_csv']
    del compression_kwargs['output_dir']

    print(f"--- Starting Multi-Scene Compression ---\n"
          f"Input Video: {input_file.name}\n"
          f"Scenes Found: {len(scenes_data)}\n"
          f"Output Directory: {output_dir.resolve()}\n"
          f"Default Compression Args: {compression_kwargs}")
    print("----------------------------------------")

    # Collect all start times for calculation
    start_times = [float(row['Start Time (seconds)']) for row in scenes_data]

    # Iterate and process scenes
    for i, scene in enumerate(scenes_data):
        scene_number = scene['Scene Number']
        start_time_sec = start_times[i]

        # Calculate End Time: Use the start time of the next scene.
        if i + 1 < len(start_times):
            end_time_sec = start_times[i+1]
        else:
            # For the last scene, use the official 'End Time (seconds)' column
            end_time_sec = float(scene['End Time (seconds)'])

        # Convert times to string format for FFmpeg -ss / -to parameters
        start_time_str = f"{start_time_sec:.3f}"
        end_time_str = f"{end_time_sec:.3f}"

        # Construct output filename: [INPUT_BASE]-S[SCENE_NUM].webm
        base_name = input_file.stem
        output_file_name = f"{base_name}-S{scene_number}.webm"
        output_path = output_dir / output_file_name

        print(f"\n========================================")
        print(f"Processing Scene {scene_number} ({start_time_str}s to {end_time_str}s)")
        print(f"Output: {output_path.name}")
        print(f"========================================")

        # Call the core compress_video function
        final_output_file, final_size_mib = compress_video(
            input_file=str(input_file),
            output_file=str(output_path),
            start=start_time_str,
            end=end_time_str,
            # info_detail is automatically False here, keeping output clean
            **compression_kwargs
        )

        if final_output_file:
            print(f"Scene {scene_number} SUCCESS: {final_size_mib:.2f} MiB")
        else:
            print(f"Scene {scene_number} FAILED.")

if __name__ == '__main__':
    try:
        run_scene_compression()
    except ScriptError as e:
        sys.stderr.write(f"\nCritical Error: {e}\n")
        sys.exit(1)
