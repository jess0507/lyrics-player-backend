"""對齊用正規化與 /align 切長行的純邏輯單元測試(不需 whisperx / torch / opencc):

    python -m unittest test_lyrics_text
"""

import unittest

from lyrics_text import (
    MASK_CHAR,
    SCRIPT_SIMPLIFIED,
    SCRIPT_TRADITIONAL,
    convert_script,
    output_script_for,
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
    params = dict(max_chars_cjk=4, max_words=3, max_seconds=100.0)
    params.update(kw)
    return split_aligned_lines(lines, chars, sep, **params)


class SplitAlignedLinesTest(unittest.TestCase):
    def test_short_lines_kept_with_clause_punct(self):
        lines = ["我愛，你"]
        chars = _timed("我愛，你")
        new_lines, new_chars = _split(lines, chars, "")
        self.assertEqual(new_lines, lines)
        self.assertEqual(new_chars, chars)

    def test_sentence_end_always_splits_even_if_short(self):
        line = "好。走吧！"
        new_lines, new_chars = _split([line], _timed(line), "")
        self.assertEqual(new_lines, ["好。", "走吧！"])
        self.assertEqual(len(new_chars), len("".join(new_lines)))

    def test_fullwidth_period_and_interrobang_split(self):
        line = "好．走吧‼來"
        new_lines, _ = _split([line], _timed(line), "")
        self.assertEqual(new_lines, ["好．", "走吧‼", "來"])

    def test_trailing_sentence_end_keeps_line(self):
        line = "我愛你。"
        new_lines, _ = _split([line], _timed(line), "")
        self.assertEqual(new_lines, [line])

    def test_long_cjk_line_split_at_clause_punct(self):
        line = "我愛你，你愛我"
        new_lines, new_chars = _split([line], _timed(line), "")
        self.assertEqual(new_lines, ["我愛你，", "你愛我"])
        self.assertEqual(len(new_chars), len("".join(new_lines)))

    def test_long_line_clauses_greedily_merged(self):
        # 上限 4 字:「一，二，」可併成一行,不會切成四行碎片。
        line = "一，二，三四，五"
        new_lines, _ = _split([line], _timed(line), "")
        self.assertEqual(new_lines, ["一，二，", "三四，五"])

    def test_long_line_without_punct_not_split(self):
        line = "一二三四五六"
        new_lines, _ = _split([line], _timed(line), "")
        self.assertEqual(new_lines, [line])

    def test_enumeration_comma_not_a_break(self):
        line = "春、夏、秋、冬"
        new_lines, _ = _split([line], _timed(line), "")
        self.assertEqual(new_lines, [line])

    def test_punct_never_starts_line(self):
        line = "一二三四，」五"
        new_lines, _ = _split([line], _timed(line), "")
        self.assertEqual(new_lines, ["一二三四，」", "五"])

    def test_spaced_sentence_split_preserves_separator_chars(self):
        line = "a b. c d"
        chars = [
            _c("a", 0.0, 0.1), _c(" "), _c("b", 0.2, 0.3), _c("."), _c(" "),
            _c("c", 2.0, 2.1), _c(" "), _c("d", 2.2, 2.3),
        ]
        lines = ["x", line]
        all_chars = [_c("x", 0.0, 0.0), _c(" ")] + chars
        new_lines, new_chars = _split(lines, all_chars, " ", max_words=10)
        self.assertEqual(new_lines, ["x", "a b.", "c d"])
        self.assertEqual(len(new_chars), len(" ".join(new_lines)))
        # "c" 所在新行的首字時間正確對回。
        offset = len("x a b. ")
        self.assertEqual(new_chars[offset]["start"], 2.0)

    def test_ellipsis_and_decimal_are_not_sentence_ends(self):
        self.assertEqual(_split(["oh... yeah"], _timed("oh... yeah"), " ")[0], ["oh... yeah"])
        self.assertEqual(_split(["我3.5你"], _timed("我3.5你"), "")[0], ["我3.5你"])

    def test_long_pause_splits_even_short_line(self):
        line = "a b c"
        chars = [_c("a", 0.0, 0.1), _c(" "), _c("b", 0.2, 0.3), _c(" "), _c("c", 5.0, 5.1)]
        new_lines, new_chars = _split([line], chars, " ")
        self.assertEqual(new_lines, ["a b", "c"])
        self.assertEqual(len(new_chars), len(" ".join(new_lines)))

    def test_short_pause_in_short_line_not_split(self):
        line = "a b c"
        chars = [_c("a", 0.0, 0.1), _c(" "), _c("b", 0.2, 0.3), _c(" "), _c("c", 1.0, 1.1)]
        self.assertEqual(_split([line], chars, " ")[0], [line])

    def test_long_line_split_at_pause(self):
        # 6 字 > 上限 4,無標點;「四」前停頓 0.8s 可作為斷點。
        line = "一二三四五六"
        chars = _timed("一二三") + _timed("四五六", t0=1.4)
        new_lines, _ = _split([line], chars, "")
        self.assertEqual(new_lines, ["一二三", "四五六"])

    def test_pause_on_untimed_char_ignored(self):
        # 無時間的字不算停頓(以前一個有時間的字為準)。
        line = "一二X三"
        chars = [_c("一", 0.0, 0.1), _c("二", 0.2, 0.3), _c("X"), _c("三", 0.4, 0.5)]
        self.assertEqual(_split([line], chars, "", max_chars_cjk=20)[0], [line])

    def test_split_on_duration_at_clause_punct(self):
        line = "一二，三"
        chars = [_c("一", 0.0, 1.0), _c("二", 1.1, 2.0), _c("，"), _c("三", 2.1, 5.0)]
        new_lines, _ = _split([line], chars, "", max_chars_cjk=10, max_seconds=3.0)
        self.assertEqual(new_lines, ["一二，", "三"])

    def test_mixed_english_word_in_cjk_not_cut(self):
        line = "我說hello你好嗎"
        new_lines, _ = _split([line], _timed(line), "")
        self.assertIn("hello", "".join(new_lines))
        self.assertTrue(any("hello" in l for l in new_lines))

    def test_script_switch_does_not_split(self):
        # 對齊模型一次只處理一種語言:語系切換(行首 / 行尾 / 句中的外文)都不是斷點。
        for line in [
            "Apple蘋果",
            "Baby 我想你",
            "我愛你baby",
            "520我愛你",
            "給我一個kiss吧",
            "我說hello你好嗎",
        ]:
            new_lines, new_chars = _split([line], _timed(line), "", max_chars_cjk=20)
            self.assertEqual(new_lines, [line], line)
            self.assertEqual(len(new_chars), len(line))

    def test_script_switch_in_spaced_language_does_not_split(self):
        for line in ["I love you 我愛你", "Absorb,吸收."]:
            new_lines, new_chars = _split([line], _timed(line), " ", max_words=10)
            self.assertEqual(new_lines, [line], line)
            self.assertEqual(len(new_chars), len(line))

    def test_sentence_split_keeps_mixed_script_together(self):
        line = "好。Apple蘋果"
        new_lines, _ = _split([line], _timed(line), "", max_chars_cjk=20)
        self.assertEqual(new_lines, ["好。", "Apple蘋果"])

    def test_abbreviations_are_not_sentence_ends(self):
        for line in [
            "Mr. Jackson agreed",
            "Dr. Smith was here",
            "the U.S. economy",
            "J. K. Rowling wrote",
        ]:
            self.assertEqual(_split([line], _timed(line), " ", max_words=10)[0], [line], line)

    def test_normal_word_before_period_still_ends_sentence(self):
        for line, expected in [
            ("I am. You are", ["I am.", "You are"]),
            ("so am I. Me too", ["so am I.", "Me too"]),
            ("I said no. Go", ["I said no.", "Go"]),
        ]:
            self.assertEqual(
                _split([line], _timed(line), " ", max_words=10)[0], expected, line
            )

    def test_length_mismatch_returns_input(self):
        lines = ["abc"]
        chars = [_c("a")]
        self.assertEqual(_split(lines, chars, " "), (lines, chars))



class OutputScriptTest(unittest.TestCase):
    def test_traditional_hints(self):
        for hint in ["zh-TW", "zh_HK", "zh-Hant", "zh-Hant-TW", "yue"]:
            self.assertEqual(output_script_for(hint), SCRIPT_TRADITIONAL, hint)

    def test_simplified_hints(self):
        for hint in ["zh-CN", "zh-Hans", "zh-Hans-CN", "zh-SG"]:
            self.assertEqual(output_script_for(hint), SCRIPT_SIMPLIFIED, hint)

    def test_ambiguous_or_other_is_none(self):
        for hint in [None, "", "zh", "en", "ja", "zh-twx"]:
            self.assertIsNone(output_script_for(hint), hint)

    def test_convert_none_is_identity(self):
        self.assertEqual(convert_script("放弃", None), "放弃")


try:
    import opencc  # noqa: F401
    _HAS_OPENCC = True
except ImportError:
    _HAS_OPENCC = False


@unittest.skipUnless(_HAS_OPENCC, "需要 opencc(容器內有)")
class ConvertScriptOpenCCTest(unittest.TestCase):
    def test_simplified_to_traditional_whole_line(self):
        self.assertEqual(convert_script("头发很长", SCRIPT_TRADITIONAL), "頭髮很長")

    def test_traditional_to_simplified(self):
        self.assertEqual(convert_script("頭髮很長", SCRIPT_SIMPLIFIED), "头发很长")

    def test_mixed_line_unified(self):
        self.assertEqual(convert_script("Abuse 濫用 虐待", SCRIPT_TRADITIONAL), "Abuse 濫用 虐待")
        self.assertEqual(convert_script("Abandon 放弃", SCRIPT_TRADITIONAL), "Abandon 放棄")


if __name__ == "__main__":
    unittest.main()
