"""歌詞文字處理 / 顯示行 ↔ 對齊行對應的單元測試。不需 aeneas / ffmpeg / GCS:

    python -m unittest test_lyrics_text

aligner 只在實際對齊時才 import aeneas,所以 detect_language /
assemble_fragments 也能在此測。
"""

import unittest

from aligner import assemble_fragments, detect_language
from lyrics_text import (
    is_annotation_line,
    mask_inline,
    plan_lines,
    script_units,
    split_spans,
    tts_text,
)


def texts(line, **kw):
    """單行 → 顯示行文字清單。"""
    rows, _ = plan_lines([line], **kw)
    return [r["text"] for r in rows]


class AnnotationTest(unittest.TestCase):
    def test_annotation_lines(self):
        for line in ["[Chorus]", "(副歌)", "【Verse 2】", "Chorus:", "Verse 1",
                     "作詞：王小明", "Lyrics by: Someone", "[ar:Someone]"]:
            self.assertTrue(is_annotation_line(line), line)

    def test_lyric_lines_not_annotation(self):
        for line in ["我愛你", "Hello world", "Break my heart"]:
            self.assertFalse(is_annotation_line(line), line)

    def test_annotation_not_sent_to_aeneas(self):
        rows, align_texts = plan_lines(["[Chorus]", "我愛你", "作詞：王小明", "你好"])
        self.assertEqual([r["text"] for r in rows], ["[Chorus]", "我愛你", "作詞：王小明", "你好"])
        self.assertEqual([r["align"] for r in rows], [None, 0, None, 1])
        self.assertEqual(align_texts, ["我愛你", "你好"])

    def test_inline_removed_only_from_tts(self):
        rows, align_texts = plan_lines(["[01:23.45]我愛你 (x2)"])
        self.assertEqual(rows, [{"text": "[01:23.45]我愛你 (x2)", "align": 0}])
        self.assertEqual(align_texts, ["我愛你"])

    def test_leading_section_tag_removed_from_tts(self):
        self.assertEqual(tts_text(mask_inline("[Chorus] Hello")), "Hello")

    def test_repeat_marker_variants(self):
        self.assertEqual(tts_text(mask_inline("Hello ×3")), "Hello")
        self.assertEqual(tts_text(mask_inline("Hello (2x)")), "Hello")

    def test_mask_keeps_length(self):
        line = "[00:01.00]你好 (x2)"
        self.assertEqual(len(mask_inline(line)), len(line))

    def test_nfkc_for_tts_only(self):
        rows, align_texts = plan_lines(["ＡＢＣ"])
        self.assertEqual(rows[0]["text"], "ＡＢＣ")
        self.assertEqual(align_texts, ["ABC"])

    def test_line_without_speech_untimed(self):
        rows, align_texts = plan_lines(["(x2)", "你好"])
        self.assertEqual([r["align"] for r in rows], [None, 0])
        self.assertEqual(align_texts, ["你好"])

    def test_all_annotations_gives_no_align_texts(self):
        rows, align_texts = plan_lines(["[Chorus]", "作詞：xxx"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(align_texts, [])

    def test_blank_lines_dropped(self):
        rows, align_texts = plan_lines(["", "  ", "你好"])
        self.assertEqual(rows, [{"text": "你好", "align": 0}])


class SentenceSplitTest(unittest.TestCase):
    def test_sentence_end_punct(self):
        self.assertEqual(texts("第一句。第二句！"), ["第一句。", "第二句！"])
        self.assertEqual(texts("Hello. World"), ["Hello.", "World"])
        self.assertEqual(texts("Really?! Yes"), ["Really?!", "Yes"])

    def test_ellipsis_not_sentence_end(self):
        self.assertEqual(texts("wait... then"), ["wait... then"])

    def test_decimal_not_sentence_end(self):
        self.assertEqual(texts("3.5 stars"), ["3.5 stars"])

    def test_abbreviations_not_sentence_end(self):
        self.assertEqual(texts("Mr. Jackson said hi"), ["Mr. Jackson said hi"])
        self.assertEqual(texts("U.S. army"), ["U.S. army"])
        self.assertEqual(texts("J. K. Rowling"), ["J. K. Rowling"])

    def test_i_and_no_are_sentence_end(self):
        self.assertEqual(texts("so am I. You"), ["so am I.", "You"])
        self.assertEqual(texts("I said no. Go"), ["I said no.", "Go"])

    def test_inline_timestamp_dot_not_split(self):
        rows, align_texts = plan_lines(["[01:23.45]你好嗎？我很好"])
        self.assertEqual([r["text"] for r in rows], ["[01:23.45]你好嗎？", "我很好"])
        self.assertEqual(align_texts, ["你好嗎?", "我很好"])

    def test_trailing_repeat_merged_back(self):
        self.assertEqual(texts("Hello! (x2)"), ["Hello! (x2)"])

    def test_split_disabled(self):
        rows, align_texts = plan_lines(["第一句。第二句", "Apple蘋果"], split=False)
        self.assertEqual([r["text"] for r in rows], ["第一句。第二句", "Apple蘋果"])
        self.assertEqual(len(align_texts), 2)


class LongLineSplitTest(unittest.TestCase):
    def test_short_line_keeps_clause_punct(self):
        self.assertEqual(texts("短句，也不切"), ["短句，也不切"])

    def test_long_cjk_line_split_at_clause_punct(self):
        self.assertEqual(
            texts("我們一起走過的那些日子，如今都已經變成回憶，再也回不去了"),
            ["我們一起走過的那些日子，", "如今都已經變成回憶，", "再也回不去了"],
        )

    def test_greedy_merge_clauses(self):
        self.assertEqual(
            texts("我想你，我愛你，我想你，我愛你，我想你，我愛你"),
            ["我想你，我愛你，我想你，我愛你，", "我想你，我愛你"],
        )

    def test_long_line_without_clause_punct_kept(self):
        line = "我們一起走過的那些日子如今都已經變成回憶"
        self.assertEqual(texts(line), [line])

    def test_long_spaced_line_by_words(self):
        self.assertEqual(
            texts("one two three four five, six seven eight nine ten eleven, twelve"),
            ["one two three four five,", "six seven eight nine ten eleven, twelve"],
        )

    def test_custom_limits(self):
        self.assertEqual(
            split_spans("一二三，四五六", max_chars_cjk=3),
            [(0, 4), (4, 7)],
        )


class ScriptSplitTest(unittest.TestCase):
    def test_script_switch_does_not_split(self):
        # aeneas 一次只用一種語言合成:語系切換不是斷點。
        for line in ["Apple蘋果", "Absorb,吸收.", "我愛你baby", "給我一個kiss吧", "2024我們"]:
            self.assertEqual(texts(line), [line], line)

    def test_leading_section_tag_line_stays_whole(self):
        rows, align_texts = plan_lines(["[Chorus] Baby 我想你"])
        self.assertEqual([r["text"] for r in rows], ["[Chorus] Baby 我想你"])
        self.assertEqual(align_texts, ["Baby 我想你"])


class ScriptUnitsTest(unittest.TestCase):
    def test_latin_counted_by_word(self):
        self.assertEqual(script_units(["I love you"])["latin"], 3)
        self.assertEqual(script_units(["rock-n-roll t'aime"])["latin"], 2)

    def test_cjk_counted_by_char(self):
        counts = script_units(["我愛你", "사랑해", "すき"])
        self.assertEqual((counts["han"], counts["hangul"], counts["kana"]), (3, 3, 2))

    def test_lines_scanned_separately(self):
        self.assertEqual(script_units(["love", "you"])["latin"], 2)


class DetectLanguageTest(unittest.TestCase):
    def test_hangul_overrides_zh_hint(self):
        self.assertEqual(detect_language(["사랑해 너를"], "zh-TW"), "kor")

    def test_kana_is_japanese(self):
        self.assertEqual(detect_language(["君が好き"], "zh"), "jpn")

    def test_han_with_english_chorus(self):
        self.assertEqual(detect_language(["我愛你 baby"], "en"), "cmn")

    def test_han_keeps_yue_hint(self):
        self.assertEqual(detect_language(["你好"], "yue"), "yue")

    def test_latin_with_non_latin_hint(self):
        self.assertEqual(detect_language(["Hello world"], "zh-TW"), "eng")

    def test_latin_keeps_latin_hint(self):
        self.assertEqual(detect_language(["Hola amigo"], "es"), "spa")

    def test_undetermined_uses_hint(self):
        self.assertEqual(detect_language(["123"], "ja"), "jpn")


class AssembleFragmentsTest(unittest.TestCase):
    def test_untimed_rows_inherit_previous_begin(self):
        rows = [
            {"text": "[Chorus]", "align": None},
            {"text": "第一句。", "align": 0},
            {"text": "作詞：xxx", "align": None},
            {"text": "第二句", "align": 1},
        ]
        aligned = [{"begin": 1.5, "end": 3.0}, {"begin": 3.0, "end": 5.0}]
        frags = assemble_fragments(rows, aligned)
        self.assertEqual([f["begin"] for f in frags], [0.0, 1.5, 1.5, 3.0])
        self.assertEqual([f["text"] for f in frags], [r["text"] for r in rows])
        self.assertEqual([f["aligned"] for f in frags], [False, True, False, True])
        self.assertEqual([f["index"] for f in frags], [0, 1, 2, 3])
        self.assertEqual(frags[2]["end"], frags[2]["begin"])


if __name__ == "__main__":
    unittest.main()
