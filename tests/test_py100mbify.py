import unittest
# Importing the actual function name 'calculate_bitrates'
from py100mbify import calculate_bitrates, get_time_in_seconds
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

    def test_calculate_bitrates_no_audio(self):
        """
        Verifies bitrate calculation when audio is muted (0 kbps).
        Formula: (Target MiB * 8192 - (Audio kbps * Duration)) / Duration
        """
        target_mib = 10
        duration_sec = 10.0
        audio_kbps = 0

        # FIX: Unpack the result tuple, assuming video bitrate is the first element
        # The *rest captures any other return values (like total bitrate)
        video_bitrate, *rest = calculate_bitrates(duration_sec, target_mib, audio_kbps, False)
        self.assertAlmostEqual(video_bitrate, 8192.0, places=1)

    def test_calculate_bitrates_with_audio(self):
        """
        Verifies bitrate calculation with a standard 96 kbps audio track.
        """
        target_mib = 10
        duration_sec = 10.0
        audio_kbps = 96

        # Expected calculation: 8096.0 kbps
        # FIX: Unpack the result tuple, checking only the video bitrate
        video_bitrate, *rest = calculate_bitrates(duration_sec, target_mib, audio_kbps, True)
        self.assertAlmostEqual(video_bitrate, 8096.0, places=1)

    def test_bitrate_too_low(self):
        """
        Test case where the target size is too small, resulting in a video bitrate
        lower than the minimum allowed (MIN_VIDEO_BITRATE_KBPS = 50).
        """
        target_mib = 0.1
        duration_sec = 100.0
        audio_kbps = 96

        # The function should return MIN_VIDEO_BITRATE_KBPS (50)
        # FIX: Unpack the result tuple, checking only the video bitrate
        video_bitrate, *rest = calculate_bitrates(duration_sec, target_mib, audio_kbps, True)
        self.assertAlmostEqual(video_bitrate, 50.0, places=1)

if __name__ == '__main__':
    unittest.main()
