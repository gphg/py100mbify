import unittest

from py100mbify.overlay_cli import extract_overlays, parse_overlay, transform_ffmpeg_command


class TestOverlayArguments(unittest.TestCase):
    def test_parse_path_and_optional_filter(self):
        self.assertEqual(parse_overlay("logo.png"), ("logo.png", ""))
        self.assertEqual(
            parse_overlay("logo.png;overlay=x=(W-w)/2:y=(H-h)-52"),
            ("logo.png", "overlay=x=(W-w)/2:y=(H-h)-52"),
        )

    def test_extracts_repeated_options_in_order(self):
        remaining, overlays = extract_overlays([
            "input.mp4", "--overlay", "first.png", "--size", "50",
            "--overlay=second.png;overlay=x=10:y=20",
        ])
        self.assertEqual(remaining, ["input.mp4", "--size", "50"])
        self.assertEqual(overlays, [
            ("first.png", ""),
            ("second.png", "overlay=x=10:y=20"),
        ])

    def test_rejects_empty_overlay_values(self):
        for value in ("", ";overlay=x=1", "logo.png;"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_overlay(value)

    def test_adds_inputs_and_chains_overlays_in_order(self):
        command = [
            "ffmpeg", "-hide_banner", "-i", "input.mp4", "-vf", "scale=640:-2",
            "-c:v", "libx264", "-map", "0:v:0", "-an", "output.mp4",
        ]
        transformed = transform_ffmpeg_command(command, [
            ("first.png", ""),
            ("second.png", "overlay=x=10:y=20"),
        ])
        self.assertIn("-loop", transformed)
        self.assertEqual(transformed.count("-i"), 3)
        graph = transformed[transformed.index("-filter_complex") + 1]
        self.assertIn("[0:v:0]scale=640:-2[vbase]", graph)
        self.assertIn("[vbase][1:v:0]overlay[overlay0]", graph)
        self.assertIn("[overlay0][2:v:0]overlay=x=10:y=20[overlay1]", graph)
        self.assertEqual(transformed[transformed.index("-map") + 1], "[overlay1]")
        self.assertNotIn("-vf", transformed)

    def test_appends_overlay_to_existing_complex_graph(self):
        command = [
            "ffmpeg", "-i", "input.mp4", "-filter_complex",
            "[0:v]trim=start=1:end=2,setpts=PTS-STARTPTS[vseg]",
            "-map", "[vseg]", "-map", "0:a:0", "output.mp4",
        ]
        transformed = transform_ffmpeg_command(command, [("logo.png", "")])
        graph = transformed[transformed.index("-filter_complex") + 1]
        self.assertIn("[vseg][1:v:0]overlay[overlay0]", graph)
        self.assertEqual(transformed[transformed.index("-map") + 1], "[overlay0]")
        # Audio mapping is not changed.
        self.assertEqual(transformed[transformed.index("-map", transformed.index("-map") + 1) + 1], "0:a:0")


if __name__ == "__main__":
    unittest.main()
