#!/usr/bin/env python

import argparse
import csv
import os
import sys
from pathlib import Path
# Import necessary constants and functions for logic
from py100mbify import compress_video, ScriptError, DEFAULT_TARGET_SIZE_MIB, DEFAULT_AUDIO_BITRATE_KBPS, get_video_info, get_time_in_seconds

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
    runner_parser.add_argument('--print', action='store_true',
                               help='Do not run FFmpeg. Instead, print the py100mbify command for each scene to stdout.')

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
    compress_parser.add_argument('--proto', action='store_true',
                                 help='Prototype mode: Use fast, low-quality single-pass CRF encoding to quickly test clipping accuracy.')

    # Final comprehensive parse to ensure all arguments are captured in one object
    final_args = compress_parser.parse_args(argv)
    return final_args

def run_scene_compression():
    """
    Reads CSV, prepares arguments for each scene, and either runs compression or prints commands.
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

    # Create output directory only if we are actually encoding
    if not args.print:
        output_dir.mkdir(exist_ok=True)


    # --- 1. Reconstruct common command line arguments ---
    common_args_list = []

    # Iterate through all arguments passed to scene_runner.py
    for key, value in vars(args).items():
        # Skip internal runner args
        if key in ['input_video', 'scenes_csv', 'output_dir', 'print']:
            continue

        arg_name = f'--{key.replace("_", "-")}'

        # Handle boolean flags (only add if True)
        if isinstance(value, bool):
            if value:
                common_args_list.append(arg_name)
            continue

        # Handle arguments that are None
        if value is None:
            continue

        # Handle arguments with default values (skip if default)
        is_default = False
        if key == 'size' and value == DEFAULT_TARGET_SIZE_MIB:
            is_default = True
        elif key == 'audio_bitrate' and value == DEFAULT_AUDIO_BITRATE_KBPS:
            is_default = True
        elif key == 'speed' and value == 1.0:
            is_default = True

        if is_default:
            continue

        # Quote values for robustness in shell environment (e.g., filters, paths)
        # We assume the user has 'py100mbify' in their PATH or environment
        if isinstance(value, str):
            common_args_list.extend([arg_name, f'"{value}"'])
        else:
            common_args_list.extend([arg_name, str(value)])

    # --- 2. Process Scenes (Print or Run) ---

    start_times = [float(row['Start Time (seconds)']) for row in scenes_data]

    if not args.print:
        print(f"--- Starting Multi-Scene Compression ---\n"
              f"Input Video: {input_file.name}\n"
              f"Scenes Found: {len(scenes_data)}\n"
              f"Output Directory: {output_dir.resolve()}")
        if args.proto:
             print("WARNING: Running in PROTO Mode. Output quality will be low but encoding will be fast.")
        print("----------------------------------------")


    for i, scene in enumerate(scenes_data):
        scene_number_raw = scene['Scene Number']

        try:
            scene_num_int = int(scene_number_raw)
            # Format scene number with 3 leading zeros (S001, S010, S123)
            formatted_scene_number = f'{scene_num_int:03d}'
        except ValueError:
            formatted_scene_number = scene_number_raw

        start_time_sec = start_times[i]

        # Calculate End Time: Use the start time of the next scene or the end of the video
        if i + 1 < len(start_times):
            end_time_sec = start_times[i+1]
        else:
            end_time_sec = float(scene['End Time (seconds)'])

        clip_duration_sec = end_time_sec - start_time_sec

        # Format times for FFmpeg
        start_time_str = f"{start_time_sec:.3f}"
        end_time_str = f"{end_time_sec:.3f}"

        # Construct output filename: [INPUT_BASE]-S[SCENE_NUM].webm
        base_name = input_file.stem
        proto_suffix = "-PROTO" if args.proto else ""
        output_file_name = f"{base_name}-S{formatted_scene_number}{proto_suffix}.webm"
        output_path = output_dir / output_file_name


        if args.print:
            # --- PRINT COMMAND MODE ---
            # Command starts with 'py100mbify' (assumed executable name)
            command = ['py100mbify']

            # Input file and output path must be quoted for shell safety
            command.append(f'"{input_file.name}"')
            command.append(f'"{output_path}"')

            # Scene-specific trim arguments
            command.extend(['--start', start_time_str])
            command.extend(['--end', end_time_str])

            # Append all common arguments
            command.extend(common_args_list)

            # Print the final command line to stdout
            print(' '.join(command))

        else:
            # --- NORMAL EXECUTION MODE ---
            print(f"\n========================================")
            print(f"Processing Scene {formatted_scene_number} ({start_time_str}s for {clip_duration_sec:.3f}s)")
            print(f"Output: {output_path.name}")
            print(f"========================================")

            # Note: compression_kwargs is NOT needed here, we just need the values from args
            final_output_file, final_size_mib = compress_video(
                input_file=str(input_file),
                output_file=str(output_path),
                start=start_time_str,
                end=end_time_str,
                size=args.size,
                audio_bitrate=args.audio_bitrate,
                mute=args.mute,
                speed=args.speed,
                fps=args.fps,
                scale=args.scale,
                cpu_priority=args.cpu_priority,
                prepend_filters=args.prepend_filters,
                append_filters=args.append_filters,
                rotate=args.rotate,
                keep_metadata=args.keep_metadata,
                proto=args.proto,
                info_detail=False # Keep false to avoid redundant prints for every scene
            )

            if final_output_file:
                print(f"Scene {formatted_scene_number} SUCCESS: {final_size_mib:.2f} MiB")
            else:
                print(f"Scene {formatted_scene_number} FAILED.")

if __name__ == '__main__':
    try:
        run_scene_compression()
    except ScriptError as e:
        sys.stderr.write(f"\nCritical Error: {e}\n")
        sys.exit(1)
