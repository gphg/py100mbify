# py100mbify

Based on: [100mbify.sh](https://gist.github.com/gphg/b1b0dc152bf60a606afd6dbf55c33319).
Rewrite in Python.

A command-line tool written in Python to compress video files to a precise target size, built primarily for creating high-quality WebM (VP9/Opus) files for platforms with file size limits like Discord.

The core of this script is a powerful 2-pass VP9 encoding routine wrapped around FFmpeg, allowing you to hit a file size target (e.g., 100 MiB) while maintaining the best possible quality.

## Features

* **Target Size Compression:** Calculate the exact video bitrate needed to hit a specified file size (e.g., `--size 50` MiB).
* **WebM (VP9/Opus):** Optimized for creating modern, efficient WebM files.
* **Audit & History:** Automatically logs completion status, final size, and encoding speed to `py100mbify_history.log` for long-term tracking.
* **Smart Scaling:** Automatically detects integer-ratio scaling (e.g., 4K -> 1080p) and uses `neighbor` for pixel-perfect sharpness, or falls back to `bicubic`.
* **Trimming & Manipulation:** Supports trimming (`--start`, `--end`), scaling (`--scale`), speed adjustment (`--speed`), and rotation.
* **Hardsub Support:** Easily burn in subtitles from the input file, automatically handling sync even when trimming mid-video.
* **Web Compatibility Mode:** One-flag fix (`--target-web`) to force 8-bit color and Profile 0.
* **Prototype Mode:** Quickly test cuts and filters using a fast single-pass CRF encode (`--proto`).
* **Command Inspection:** Use `--print` to output the exact FFmpeg commands for debugging.

## Installation and Setup

### Prerequisites

You must have FFmpeg and FFprobe installed and accessible in your system's `$PATH`.

* **Windows:** Use [Scoop](https://scoop.sh/) (`scoop install ffmpeg`) or manually add to `%PATH%`.
* **Linux:** `sudo apt install ffmpeg`.

Optional: Install `psutil` via pip if you wish to use the `--cpu-priority` feature on Windows.

### Installation

Navigate to the root directory and install in editable mode:

```bash
pip install -e .
```

## Usage

### Core Compression Command

| Argument | Description | Example |
| :--- | :--- | :--- |
| `input_file` | Path to the source video file. **Required**. | `source.mp4` |
| `output_file` | Output path. Defaults to the **current working directory** using the input's filename with a `.webm` extension. | `final.webm` |

### Command Categories

The CLI is organized into functional groups for better clarity:

#### 1. Target Options

| Argument | Description | Default |
| --- | --- | --- |
| `--size` | Target output size in MiB. | 100.0 |
| `--audio-bitrate` | Bitrate for the libopus audio stream in kbps. | 192 |
| `--mute` | Completely strip audio from the output. | False |

#### 2. Clipping & Transformation

| Argument | Description | Example |
| --- | --- | --- |
| `--start` | Start offset (HH:MM:SS.mmm or seconds). | `--start 00:01:30` |
| `--end` | End timestamp (HH:MM:SS.mmm or seconds). | `--end 120` |
| `--speed` | Playback speed multiplier (e.g., 2.0). | `--speed 1.5` |
| `--scale` | Resizes the smallest dimension (maintains aspect). | `--scale 720` |
| `--rotate` | Rotate video clockwise by degrees. | `--rotate 90` |

#### 3. Quality & Filtering

| Argument | Description |
| --- | --- |
| `--hard-sub` | Burn subtitles from the input file. |
| `--target-web` | Force 8-bit color (Profile 0) for browser compatibility. |
| `--prepend-filters` | FFmpeg video filters to apply BEFORE scaling. |
| `--append-filters` | FFmpeg video filters to apply AFTER internal logic. |

#### 4. Execution Control

| Argument | Description |
| --- | --- |
| `--cpu-priority` | Set process priority (`low` or `high`). |
| `--proto [CRF]` | Fast single-pass encode for testing. Default CRF: 30. |
| `--print` | Print FFmpeg commands to stdout without running them. |

## Informative Reporting

For long-running jobs (hours or days), `py100mbify` provides detailed feedback:

* **Encoding Speed:** Calculated as a ratio (e.g., `0.50x` means encoding takes twice as long as the video duration).
* **Accuracy:** Compares final file size against the requested target.
* **History Log:** Check `py100mbify_history.log` for a persistent audit trail of all completed encodes. Logs are stored in the same directory as the output file to ensure write permissions.

## License

None. Use at your own risk!
