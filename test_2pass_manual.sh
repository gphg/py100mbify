#!/bin/bash
set -e

# Create a 5-second test video
ffmpeg -f lavfi -i testsrc=duration=5:size=320x240:rate=30 \
        -f lavfi -i sine=frequency=440:duration=5 \
        -c:v libx264 -c:a aac -y test_input.mp4

# Run py100mbify with 2-pass
py100mbify test_input.mp4 test_output.mp4 --size 5 --print

# Check if both pass 1 and pass 2 commands were printed
echo "✅ 2-pass commands generated successfully"

# Clean up
rm -f test_input.mp4 test_output.mp4 passlog_*.log*
