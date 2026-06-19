Title: fix(proto+seek): precise seeking and proto trim adjustments

This PR makes two related changes to improve clipping accuracy and avoid
bleeding the first frame of the next scene when cutting multiple clips:

1) Frame-accurate seeking: place -ss after -i in FFmpeg command construction
   so that ffmpeg decodes to the exact requested start timestamp. This
   guarantees precise clip boundaries at the cost of slower start/seek time.

2) (Historical) A proto-mode epsilon trim was added earlier to reduce bleeding
   for prototype mode; we transitioned to frame-accurate seeking for all modes
   so the epsilon is no longer relied upon as the main mitigation.

Why:
- Fast seeking (-ss before -i) is keyframe aligned and combined with -t can
  sometimes include the next clip's first frame due to timestamp rounding.
- Using accurate seeking removes that ambiguity and ensures precise export.

Tradeoffs and test notes:
- Accurate seeking is slower; prototype encodes will be slower than the prior
  fast keyframe method.
- Recommended testing steps are in the PR description (run proto_start.sh on
  a CSV and inspect the previously-problematic clips; verify -print output for
  correct -ss/-t placement; use ffprobe to check frame boundaries).

Files changed:
- src/py100mbify/__init__.py (updated ffmpeg argument ordering and comments)

Signed-off-by: Copilot <copilot@github.com>
