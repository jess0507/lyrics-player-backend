"""segment → 逐行片段 的純邏輯單元測試。

只測 :func:`transcriber.segments_to_fragments` / :func:`transcriber._clean_segment_text`
等不依賴 whisperx / torch 的部分(transcriber 對 whisperx 採延後 import):

    python -m unittest test_transcribe
"""

import unittest

from transcriber import _clean_segment_text, segments_to_fragments


def _seg(text, start=None, end=None):
    return {"text": text, "start": start, "end": end}


class CleanSegmentTextTest(unittest.TestCase):
    def test_collapses_whitespace_and_newlines(self):
        self.assertEqual(_clean_segment_text("  a\n b  c \n"), "a b c")

    def test_none_and_empty(self):
        self.assertEqual(_clean_segment_text(None), "")
        self.assertEqual(_clean_segment_text("   "), "")


class SegmentsToFragmentsTest(unittest.TestCase):
    def test_maps_segments_to_lines(self):
        segs = [_seg("hello", 0.0, 2.0), _seg("world", 2.0, 4.0)]
        frags = segments_to_fragments(segs)
        self.assertEqual([f["text"] for f in frags], ["hello", "world"])
        self.assertEqual(frags[0]["begin"], 0.0)
        self.assertEqual(frags[0]["end"], 2.0)
        self.assertEqual(frags[1]["index"], 1)

    def test_skips_empty_text_and_reindexes(self):
        segs = [_seg("a", 0.0, 1.0), _seg("   ", 1.0, 2.0), _seg("b", 2.0, 3.0)]
        frags = segments_to_fragments(segs)
        self.assertEqual([f["text"] for f in frags], ["a", "b"])
        self.assertEqual([f["index"] for f in frags], [0, 1])

    def test_missing_times_carry_forward_monotonic(self):
        segs = [_seg("a", 0.0, 2.0), _seg("b"), _seg("c", 5.0, 6.0)]
        frags = segments_to_fragments(segs)
        self.assertEqual(frags[1]["begin"], 2.0)  # 沿用前一行 end
        self.assertEqual(frags[1]["end"], 2.0)
        begins = [f["begin"] for f in frags]
        self.assertEqual(begins, sorted(begins))  # 單調遞增

    def test_clamps_backwards_start(self):
        # 段時間回退(start < 前一行 end)→ 夾住為前一行 end,保持單調。
        segs = [_seg("a", 0.0, 5.0), _seg("b", 3.0, 6.0)]
        frags = segments_to_fragments(segs)
        self.assertEqual(frags[1]["begin"], 5.0)
        self.assertEqual(frags[1]["end"], 6.0)

    def test_end_before_begin_collapses(self):
        segs = [_seg("a", 4.0, 2.0)]
        frags = segments_to_fragments(segs)
        self.assertEqual(frags[0]["begin"], 4.0)
        self.assertEqual(frags[0]["end"], 4.0)

    def test_all_empty_returns_no_fragments(self):
        self.assertEqual(segments_to_fragments([_seg(""), _seg("  ")]), [])


if __name__ == "__main__":
    unittest.main()
