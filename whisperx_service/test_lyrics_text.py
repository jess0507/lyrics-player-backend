"""對齊用正規化與 /align 切長行的純邏輯單元測試(不需 whisperx / torch / opencc):

    python -m unittest test_lyrics_text
"""

import unittest

from lyrics_text import (
    MASK_CHAR,
    is_annotation_line,
    normalize_for_alignment,
    split_aligned_lines,
)

_T2S = {"愛": "爱", "這": "这", "們": "们"}


def _t2s(ch):
    return _T2S.get(ch, ch)


def _c(ch, start=None, end=None):
    return {"char": ch, "start": start, "end": end}


def _timed(text, t0=0.0, step=0.2):
    """逐字依序給時間(空白不給時間,模擬詞界)。"""
    out, t = [], t0
    for ch in text:
        if ch == " ":
            out.append(_c(ch))
        else:
            out.append(_c(ch, t, t + step * 0.8))
            t += step
    return out


class AnnotationLineTest(unittest.TestCase):
    def test_section_tags(self):
        for line in ["[Chorus]", "(副歌)", "【Verse 2】", "Chorus:", "verse 1", "<Bridge>"]:
            self.assertTrue(is_annotation_line(line), line)

    def test_credits_and_metadata(self):
        for line in ["作詞:周杰倫", "作曲 : 某某", "Lyrics by: Someone", "Composer: X", "[ar:Artist]"]:
            self.assertTrue(is_annotation_line(line), line)

    def test_lyrics_not_annotation(self):
        for line in ["我愛你", "(oh oh oh)", "Hello, world", "作詞的人不是我", "Breaking free"]:
            self.assertFalse(is_annotation_line(line), line)


class NormalizeForAlignmentTest(unittest.TestCase):
    def test_length_preserved(self):
        for line in ["我愛你，這們", "[Chorus] hello", "ＡＢＣ x2", "[00:12.34]歌詞"]:
            self.assertEqual(len(normalize_for_alignment(line, "zh", t2s=_t2s)), len(line))

    def test_traditional_to_simplified_for_zh_only(self):
        self.assertEqual(normalize_for_alignment("我愛這們", "zh", t2s=_t2s), "我爱这们")
        self.assertEqual(normalize_for_alignment("我愛", "ja", t2s=_t2s), "我愛")

    def test_fullwidth_to_halfwidth(self):
        self.assertEqual(normalize_for_alignment("ＡＢＣ", "en"), "ABC")

    def test_whole_annotation_line_masked(self):
        self.assertEqual(normalize_for_alignment("[Chorus]", "en"), MASK_CHAR * 8)

    def test_inline_masks(self):
        self.assertEqual(
            normalize_for_alignment("[Chorus] hi", "en"), MASK_CHAR * 9 + "hi"
        )
        self.assertEqual(
            normalize_for_alignment("[00:01.00]hi", "en"), MASK_CHAR * 10 + "hi"
        )
        self.assertEqual(normalize_for_alignment("hi (x2)", "en"), "hi " + MASK_CHAR * 4)
        self.assertEqual(normalize_for_alignment("hi ×3", "en"), "hi " + MASK_CHAR * 2)

    def test_words_ending_in_x_digit_not_masked(self):
        self.assertEqual(normalize_for_alignment("box 3", "en"), "box 3")


def _split(lines, chars, sep, **kw):
    params = dict(gap_seconds=0.6, max_chars_cjk=4, max_words=3, max_seconds=100.0)
    params.update(kw)
    return split_aligned_lines(lines, chars, sep, **params)


class SplitAlignedLinesTest(unittest.TestCase):
    def test_short_lines_kept_even_with_punct(self):
        lines = ["我愛，你"]
        chars = _timed("我愛，你")
        new_lines, new_chars = _split(lines, chars, "")
        self.assertEqual(new_lines, lines)
        self.assertEqual(new_chars, chars)

    def test_long_cjk_line_split_at_punct(self):
        line = "我愛你，你愛我"
        new_lines, new_chars = _split([line], _timed(line), "")
        self.assertEqual(new_lines, ["我愛你，", "你愛我"])
        self.assertEqual(len(new_chars), len("".join(new_lines)))

    def test_long_cjk_line_hard_split_by_count(self):
        line = "一二三四五六"
        new_lines, _ = _split([line], _timed(line), "")
        self.assertEqual(new_lines, ["一二三四", "五六"])

    def test_punct_never_starts_line(self):
        line = "一二三四，五"
        new_lines, _ = _split([line], _timed(line), "")
        self.assertEqual(new_lines, ["一二三四，", "五"])

    def test_spaced_split_on_gap_and_preserves_separator_chars(self):
        line = "a b c d"
        chars = [
            _c("a", 0.0, 0.1), _c(" "), _c("b", 0.2, 0.3), _c(" "),
            _c("c", 2.0, 2.1), _c(" "), _c("d", 2.2, 2.3),
        ]
        lines = ["x", line]
        all_chars = [_c("x", 0.0, 0.0), _c(" ")] + chars
        new_lines, new_chars = _split(lines, all_chars, " ")
        self.assertEqual(new_lines, ["x", "a b", "c d"])
        self.assertEqual(len(new_chars), len(" ".join(new_lines)))
        # "c" 所在新行的首字時間正確對回。
        offset = len("x a b ")
        self.assertEqual(new_chars[offset]["start"], 2.0)

    def test_split_on_duration(self):
        line = "一二三"
        chars = [_c("一", 0.0, 1.0), _c("二", 1.1, 2.0), _c("三", 2.1, 5.0)]
        new_lines, _ = _split([line], chars, "", max_chars_cjk=10, max_seconds=3.0)
        self.assertEqual(new_lines, ["一二", "三"])

    def test_mixed_english_word_in_cjk_not_cut(self):
        line = "我說hello你好嗎"
        new_lines, _ = _split([line], _timed(line), "")
        self.assertIn("hello", "".join(new_lines))
        self.assertTrue(any("hello" in l for l in new_lines))

    def test_length_mismatch_returns_input(self):
        lines = ["abc"]
        chars = [_c("a")]
        self.assertEqual(_split(lines, chars, " "), (lines, chars))


if __name__ == "__main__":
    unittest.main()
