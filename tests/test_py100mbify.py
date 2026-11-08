import unittest

from py100mbify import calculate_bitrates, get_time_in_seconds, parse_overlay_specs


class TestPy100MbifyLogic(unittest.TestCase):
    """Tests the core calculation and parsing logic of py100mbify, independent of FFmpeg execution."""

    def test_get_time_in_seconds_parsing(self):
        self.assertEqual(get_time_in_seconds("90.5"), 90.5)
        self.assertEqual(get_time_in_seconds("00:01:30.000"), 90.0)
        self.assertEqual(get_time_in_seconds("01:00:00"), 3600.0)
        self.assertEqual(get_time_in_seconds(None), 0.0)

    def test_calculate_bitrates_no_audio(self):
        target_mib = 10
        duration_sec = 10.0
        audio_kbps = 0

        video_bitrate, *rest = calculate_bitrates(duration_sec, target_mib, audio_kbps, False)
        self.assertAlmostEqual(video_bitrate, 8192.0, places=1)

    def test_calculate_bitrates_with_audio(self):
        target_mib = 10
        duration_sec = 10.0
        audio_kbps = 96

        video_bitrate, *rest = calculate_bitrates(duration_sec, target_mib, audio_kbps, True)
        self.assertAlmostEqual(video_bitrate, 8096.0, places=1)

    def test_bitrate_too_low(self):
        target_mib = 0.1
        duration_sec = 100.0
        audio_kbps = 96

        video_bitrate, *rest = calculate_bitrates(duration_sec, target_mib, audio_kbps, True)
        self.assertAlmostEqual(video_bitrate, 50.0, places=1)

    def test_parse_overlay_specs(self):
        self.assertEqual(
            parse_overlay_specs(["logo.png", "watermark.png;overlay=x=10:y=20"]),
            [
                {"path": "logo.png", "filter": "overlay"},
                {"path": "watermark.png", "filter": "overlay=x=10:y=20"},
            ],
        )

    def test_parse_overlay_specs_rejects_empty_path(self):
        with self.assertRaises(Exception):
            parse_overlay_specs([";overlay=x=5"])


if __name__ == '__main__':
    unittest.main()
