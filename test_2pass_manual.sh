#!/bin/bash
set -e

# Create a 5-second test video
ffmpeg -f lavfi -i testsrc=duration=5:size=320x240:rate=30 \
        -f lavfi -i sine=frequency=440:duration=5 \
        -c:v libx264 -c:a aac -y test_input.mp4

# Run py100mbify with 2-pass (print)
echo ">>> Running: 2-pass commands with '--print' (no process has been made)"
py100mbify test_input.mp4 test_output.mp4 --size 5 --print


# Run py100mbify with 2-pass (verbose process)
echo ">>> Running: 2-pass commands with verbose flag on"
export PY100MBIFY_VERBOSE_2PASS=1
py100mbify test_input.mp4 test_output.mp4 --size 5

# Clean up
rm -f -v test_input.mp4 test_output.mp4 passlog_*.log*
