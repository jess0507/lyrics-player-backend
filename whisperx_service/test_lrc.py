"""LRC 組裝單元測試。不需 whisperx / torch / ffmpeg / GCS,可在任意機器上跑:

    python -m unittest test_lrc

驗證後端「對齊秒數 → LRC」這段純邏輯,與實際對齊品質無關。
"""

import unittest

from lrc import build_lrc, format_timestamp


class FormatTimestampTest(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(format_timestamp(0), "[00:00.00]")

    def test_sub_second_rounding(self):
        self.assertEqual(format_timestamp(12.345), "[00:12.34]")
        self.assertEqual(format_timestamp(12.346), "[00:12.35]")

    def test_minutes(self):
        self.assertEqual(format_timestamp(83.5), "[01:23.50]")

    def test_long_song_over_an_hour(self):
        # 73 分 09.99 秒,分鐘欄不繞回(LRC 容許 mm > 59)。
        self.assertEqual(format_timestamp(73 * 60 + 9.99), "[73:09.99]")

    def test_negative_clamped(self):
        self.assertEqual(format_timestamp(-1.0), "[00:00.00]")

    def test_centisecond_carry(self):
        # 0.999 秒四捨五入到 100 厘秒,須進位為 01.00 而非 00.100。
        self.assertEqual(format_timestamp(0.999), "[00:01.00]")


class BuildLrcTest(unittest.TestCase):
    def test_basic(self):
        fragments = [
            {"begin": 0.0, "text": "第一行"},
            {"begin": 12.34, "text": "第二行"},
        ]
        self.assertEqual(
            build_lrc(fragments),
            "[00:00.00]第一行\n[00:12.34]第二行",
        )

    def test_strips_text_whitespace(self):
        self.assertEqual(
            build_lrc([{"begin": 1.0, "text": "  hi  "}]),
            "[00:01.00]hi",
        )

    def test_missing_begin_defaults_zero(self):
        self.assertEqual(build_lrc([{"text": "x"}]), "[00:00.00]x")


if __name__ == "__main__":
    unittest.main()
