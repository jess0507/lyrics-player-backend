"""segment → 逐行片段 的純邏輯單元測試。

只測 :func:`transcriber.segments_to_fragments` / :func:`transcriber._clean_segment_text`
等不依賴 whisperx / torch 的部分(transcriber 對 whisperx 採延後 import):

    python -m unittest test_transcribe
"""

import unittest

from transcriber import (
    _clean_segment_text,
    segments_to_fragments,
    split_segments_into_lines,
)


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


def _w(word, start=None, end=None):
    return {"word": word, "start": start, "end": end}


def _split(segments, language, **kwargs):
    params = dict(gap_seconds=0.6, max_chars_cjk=4, max_words=3, max_seconds=5.0)
    params.update(kwargs)
    return split_segments_into_lines(segments, language, **params)


class SplitSegmentsIntoLinesTest(unittest.TestCase):
    def test_no_words_kept_as_is(self):
        seg = _seg("hello world", 0.0, 2.0)
        self.assertEqual(_split([seg], "en"), [seg])

    def test_splits_on_pause(self):
        seg = {
            "text": "a b c",
            "start": 0.0,
            "end": 3.0,
            "words": [_w("a", 0.0, 0.5), _w("b", 0.6, 1.0), _w("c", 2.0, 3.0)],
        }
        lines = _split([seg], "en")
        self.assertEqual([l["text"] for l in lines], ["a b", "c"])
        self.assertEqual(lines[1]["start"], 2.0)  # 新行時間取首字 start
        self.assertEqual(lines[0]["end"], 1.0)

    def test_splits_on_max_words(self):
        words = [_w(t, i * 0.3, i * 0.3 + 0.2) for i, t in enumerate("abcde")]
        lines = _split([{"text": "", "start": 0.0, "end": 2.0, "words": words}], "en")
        self.assertEqual([l["text"] for l in lines], ["a b c", "d e"])

    def test_cjk_joined_without_space_and_counted_by_chars(self):
        words = [_w(ch, i * 0.2, i * 0.2 + 0.1) for i, ch in enumerate("我愛你中國人")]
        lines = _split([{"text": "", "start": 0.0, "end": 2.0, "words": words}], "zh")
        self.assertEqual([l["text"] for l in lines], ["我愛你中", "國人"])

    def test_splits_on_max_duration(self):
        words = [_w("a", 0.0, 2.0), _w("b", 2.1, 4.0), _w("c", 4.1, 6.0)]
        lines = _split(
            [{"text": "", "start": 0.0, "end": 6.0, "words": words}], "en", max_words=10
        )
        self.assertEqual([l["text"] for l in lines], ["a b", "c"])

    def test_untimed_words_do_not_break_and_fallback_to_segment_times(self):
        # 數字等字典外字元無時間:不觸發停頓斷行,首 / 末行時間退回原段。
        words = [_w("1"), _w("2", 1.0, 1.5), _w("3")]
        lines = _split([{"text": "", "start": 0.5, "end": 2.0, "words": words}], "en")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "1 2 3")
        self.assertEqual(lines[0]["start"], 1.0)
        self.assertEqual(lines[0]["end"], 1.5)

    def test_all_untimed_uses_segment_times(self):
        words = [_w("x"), _w("y")]
        lines = _split([{"text": "", "start": 3.0, "end": 4.0, "words": words}], "en")
        self.assertEqual(lines[0]["start"], 3.0)
        self.assertEqual(lines[0]["end"], 4.0)

    def test_blank_words_skipped(self):
        words = [_w(" ", 0.0, 0.1), _w("a", 0.1, 0.2)]
        lines = _split([{"text": "", "start": 0.0, "end": 1.0, "words": words}], "en")
        self.assertEqual([l["text"] for l in lines], ["a"])

    def test_feeds_into_fragments(self):
        seg = {
            "text": "a b",
            "start": 0.0,
            "end": 3.0,
            "words": [_w("a", 0.0, 0.5), _w("b", 2.0, 3.0)],
        }
        frags = segments_to_fragments(_split([seg], "en"))
        self.assertEqual([(f["begin"], f["text"]) for f in frags], [(0.0, "a"), (2.0, "b")])


if __name__ == "__main__":
    unittest.main()
