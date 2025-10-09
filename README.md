# py100mbify

Based on: [100mbify.sh](https://gist.github.com/gphg/b1b0dc152bf60a606afd6dbf55c33319).
Rewrite in Python.

A command-line tool written in Python to compress video files to a precise target size, built primarily for creating high-quality WebM (VP9/Opus) files for platforms with file size limits.
The core of this script is a powerful 2-pass VP9 encoding routine wrapped around FFmpeg, allowing you to hit a file size target (e.g., 100 MiB) while maintaining the best possible quality.

## Features

 * Target Size Compression: Calculate the exact video bitrate needed to hit a specified file size (e.g., --size 50 MiB).
 * WebM (VP9/Opus): Optimized for creating modern, efficient WebM files.
 * Trimming & Manipulation: Supports trimming (--start, --end), scaling (--scale), speed adjustment (--speed), and custom FFmpeg filter insertion.
 * Robustness: Uses the -nostdin flag to prevent accidental keyboard input from interrupting long-running jobs.
 * Scene Processing: Includes scene_runner.py for batch processing scenes from tools like [PysceneDetect](https://github.com/Breakthrough/PySceneDetect).

## Installation and Setup

### Prerequisites
You must have FFmpeg and FFprobe installed and accessible in your system's `$PATH` (on Windows: `%PATH%`).
 * Windows/Linux/macOS: Ensure you can run `ffmpeg -version` and `ffprobe -version` successfully from your terminal.

### Option 1: Editable Installation (Recommended for Development)
This method uses your pyproject.toml file to install the package in an editable state. This allows you to run the script using the clean command py100mbify from any directory on your system.
 * Navigate to the root directory of this project (where pyproject.toml is located).
 * Install the project using pip or uv:
   ```bash
   # Use pip (standard method)
   pip install -e .

   # OR use uv (if you prefer this faster tool)
   uv pip install -e .
   ```
   The -e flag (editable) means any changes you make to the source files (__init__.py) are reflected immediately without reinstalling.
 * Run the tool from anywhere:
   ```bash
   py100mbify input.mp4 output.webm --size 50
   ```

### Option 2: Direct Script Execution (No Global Install)
If you prefer not to install the package, you can run the main script directly using Python's module execution (-m).
 * Navigate to the directory containing the py100mbify directory.
 * Run the script using the module name:
   ```
   python -m py100mbify input.mp4 output.webm --size 50
   ```
   Note: If you run this from inside the py100mbify directory, you should use `python __init__.py ...` instead.

## Usage

### Core Compression Command
The basic usage requires an input file, an output file, and the target size.
```bash
py100mbify input.mp4 output.webm --size 100
```

| Argument | Description | Example |
|---|---|---|
| input_file | Path to the video you want to compress. | video.mkv |
| output_file | Desired output path for the WebM file. | final.webm |
| --size | Target output size in MiB. (Default: 100) | --size 50 |
| --start / --end | Trimming start and end times (seconds or HH:MM:SS.ms). | --start 10 --end 30.5 |
| --scale | Resizes the smallest dimension (e.g., 720 for 720p equivalent). | --scale 1080 |
| --mute | Removes the audio track. | --mute |
| --proto | Prototype Mode: Runs a fast, low-quality single-pass encode (CRF 30) for quick testing of trimming and filtering before the slow 2-pass compression. | --proto |

### Scene Batch Processing
Use the dedicated scene_runner.py script to process a video segmented by a CSV file (e.g., from scenedetect).
```bash
python scene_runner.py \
    /path/to/video.mp4 \
    /path/to/scenes.csv \
    --output-dir ./scenes_out \
    --size 20
```

## License
None. I believe other people can code this better than me. Use at your own risk!
