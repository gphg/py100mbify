import unittest
from py100mbify import calculate_target_bitrate, get_time_in_seconds
# Note: Since the functions are in __init__.py, they are directly accessible via 'from py100mbify import ...'

class TestPy100MbifyLogic(unittest.TestCase):
    """
    Tests the core calculation and parsing logic of py100mbify,
    independent of FFmpeg execution.
    """

    def test_get_time_in_seconds_parsing(self):
        """
        Tests parsing of time strings (seconds and HH:MM:SS.ms format).
        """
        # Numeric seconds
        self.assertEqual(get_time_in_seconds("90.5"), 90.5)
        # HH:MM:SS.ms format
        self.assertEqual(get_time_in_seconds("00:01:30.000"), 90.0)
        self.assertEqual(get_time_in_seconds("01:00:00"), 3600.0)
        # Empty input
        self.assertEqual(get_time_in_seconds(None), 0.0)

    def test_calculate_target_bitrate_no_audio(self):
        """
        Verifies bitrate calculation when audio is muted (0 kbps).
        Formula: (Target MiB * 8192 - (Audio kbps * Duration)) / Duration
        """
        target_mib = 10
        duration_sec = 10.0
        audio_kbps = 0

        # Expected: (10 * 8192) / 10 = 8192.0 kbps
        result = calculate_target_bitrate(duration_sec, target_mib, audio_kbps)
        self.assertAlmostEqual(result, 8192.0, places=1)

    def test_calculate_target_bitrate_with_audio(self):
        """
        Verifies bitrate calculation with a standard 96 kbps audio track.
        """
        target_mib = 10
        duration_sec = 10.0
        audio_kbps = 96

        # Expected calculation:
        # Total kbits target: 10 * 8192 = 81920 kbits
        # Audio kbits contribution: 10 * 96 = 960 kbits
        # Video kbits target: 81920 - 960 = 80960 kbits
        # Video bitrate: 80960 / 10 = 8096.0 kbps
        result = calculate_target_bitrate(duration_sec, target_mib, audio_kbps)
        self.assertAlmostEqual(result, 8096.0, places=1)

    def test_bitrate_too_low(self):
        """
        Test case where the target size is too small, resulting in a video bitrate
        lower than the minimum allowed (MIN_VIDEO_BITRATE_KBPS = 50).
        """
        # Duration: 100s, Target MiB: 0.1, Audio kbps: 96
        # Calculation results in a video bitrate far below 50 kbps
        target_mib = 0.1
        duration_sec = 100.0
        audio_kbps = 96

        # The function should return MIN_VIDEO_BITRATE_KBPS (50)
        result = calculate_target_bitrate(duration_sec, target_mib, audio_kbps)
        self.assertAlmostEqual(result, 50.0, places=1)

if __name__ == '__main__':
    unittest.main()

