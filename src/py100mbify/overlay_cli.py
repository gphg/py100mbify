#!/usr/bin/env python
"""CLI entry point that adds ordered image overlays to py100mbify."""

import os
import shlex
import sys

import py100mbify as core


def parse_overlay(value):
    """Parse PATH[;FILTER] from one --overlay value."""
    path, separator, filter_spec = value.partition(";")
    path = path.strip()
    filter_spec = filter_spec.strip() if separator else ""
    if not path:
        raise ValueError("--overlay requires an image path before the optional ';'.")
    if separator and not filter_spec:
        raise ValueError("--overlay filter cannot be empty after ';'.")
    return path, filter_spec


def extract_overlays(argv):
    """Return (remaining argv, overlays), preserving overlay argument order."""
    remaining = []
    overlays = []
    i = 0
    while i < len(argv):
        item = argv[i]
        if item == "--overlay":
            if i + 1 >= len(argv):
                raise ValueError("--overlay requires a value.")
            overlays.append(parse_overlay(argv[i + 1]))
            i += 2
        elif item.startswith("--overlay="):
            overlays.append(parse_overlay(item.split("=", 1)[1]))
            i += 1
        else:
            remaining.append(item)
            i += 1
    return remaining, overlays


def transform_ffmpeg_command(command, overlays):
    """Add overlay inputs and chain their filters after existing video filters."""
    if not overlays or not isinstance(command, (list, tuple)) or not command or command[0] != "ffmpeg":
        return command

    cmd = list(command)
    try:
        input_pos = cmd.index("-i")
    except ValueError:
        return command

    # Add still-image inputs immediately after the source input. The image2 loop
    # option keeps each image available for the full duration of the video.
    insert_at = input_pos + 2
    input_args = []
    for path, _ in overlays:
        input_args.extend(["-loop", "1", "-i", path])
    cmd[insert_at:insert_at] = input_args

    graph_index = cmd.index("-filter_complex") if "-filter_complex" in cmd else None
    graph_parts = []
    if graph_index is not None:
        graph = cmd[graph_index + 1]
        graph_parts.append(graph)
        del cmd[graph_index:graph_index + 2]
        # The internal multi-segment graph maps its video output as [vseg] or
        # [vfinal]. Find that output mapping to connect the first overlay.
        map_positions = [i for i, value in enumerate(cmd[:-1]) if value == "-map"]
        video_map_pos = next(
            (i + 1 for i in map_positions if cmd[i + 1].startswith("[v")), None
        )
        if video_map_pos is None:
            raise ValueError("Could not find the video output of the existing filter graph.")
        current_video = cmd[video_map_pos]
    else:
        # Convert the simple -vf chain to a graph so it can be combined with
        # multiple image inputs. Existing filters remain before the overlays.
        if "-vf" in cmd:
            vf_index = cmd.index("-vf")
            existing_filters = cmd[vf_index + 1]
            del cmd[vf_index:vf_index + 2]
            graph_parts.append(f"[0:v:0]{existing_filters}[vbase]")
        else:
            graph_parts.append("[0:v:0]null[vbase]")
        map_positions = [i for i, value in enumerate(cmd[:-1]) if value == "-map"]
        video_map_pos = next(
            (i + 1 for i in map_positions if cmd[i + 1] == "0:v:0"), None
        )
        if video_map_pos is None:
            raise ValueError("Could not find the video stream mapping for overlays.")
        current_video = "[vbase]"

    overlay_input_start = 1  # input 0 is always the source video
    for index, (path, filter_spec) in enumerate(overlays):
        next_video = f"[overlay{index}]"
        filter_name = filter_spec or "overlay"
        graph_parts.append(
            f"{current_video}[{overlay_input_start + index}:v:0]"
            f"{filter_name}{next_video}"
        )
        current_video = next_video

    cmd[video_map_pos] = current_video
    filter_complex = ";".join(graph_parts)
    # Place the graph option before output mapping and codec options.
    first_map = cmd.index("-map") if "-map" in cmd else len(cmd)
    cmd[first_map:first_map] = ["-filter_complex", filter_complex]
    return cmd


def main():
    """Parse overlay options, then run the existing py100mbify CLI."""
    try:
        clean_argv, overlays = extract_overlays(sys.argv[1:])
        for path, _ in overlays:
            if not os.path.isfile(path):
                raise ValueError(f"Overlay image file not found: {path}")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)

    if not overlays:
        core.main()
        return

    original_argv = sys.argv[:]
    original_join = shlex.join
    original_run = core.subprocess.run

    def transform(command):
        try:
            return transform_ffmpeg_command(command, overlays)
        except ValueError as exc:
            raise core.ScriptError(str(exc)) from exc

    def patched_join(command):
        return original_join(transform(command))

    def patched_run(command, *args, **kwargs):
        return original_run(transform(command), *args, **kwargs)

    try:
        sys.argv = [sys.argv[0]] + clean_argv
        shlex.join = patched_join
        core.subprocess.run = patched_run
        core.main()
    finally:
        sys.argv = original_argv
        shlex.join = original_join
        core.subprocess.run = original_run


if __name__ == "__main__":
    main()
