"""segment → 逐行片段 的純邏輯單元測試。

只測 :func:`transcriber.segments_to_fragments` / :func:`transcriber._clean_segment_text`
等不依賴 whisperx / torch 的部分(transcriber 對 whisperx 採延後 import):

    python -m unittest test_transcribe
"""

import unittest

from transcriber import (
    _alignment_segments,
    _clean_segment_text,
    chars_to_words,
    collapse_repetitions,
    drop_hallucinated_segments,
    finalize_fragments,
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

    def test_long_pause_splits_even_short_line(self):
        # 停頓 2.0s ≥ 長停頓門檻(1.5s):行不長也斷。
        seg = {
            "text": "a b c",
            "start": 0.0,
            "end": 4.0,
            "words": [_w("a", 0.0, 0.5), _w("b", 0.6, 1.0), _w("c", 3.0, 4.0)],
        }
        lines = _split([seg], "en")
        self.assertEqual([l["text"] for l in lines], ["a b", "c"])
        self.assertEqual(lines[1]["start"], 3.0)  # 新行時間取首字 start
        self.assertEqual(lines[0]["end"], 1.0)

    def test_short_pause_in_short_line_not_split(self):
        words = [_w("a", 0.0, 0.5), _w("b", 1.5, 2.0)]  # 停頓 1.0s
        lines = _split([{"text": "", "start": 0.0, "end": 2.0, "words": words}], "en")
        self.assertEqual([l["text"] for l in lines], ["a b"])

    def test_sentence_end_splits(self):
        words = [_w("Hi.", 0.0, 0.3), _w("Me", 0.4, 0.6), _w("too.", 0.7, 0.9)]
        lines = _split([{"text": "", "start": 0.0, "end": 1.0, "words": words}], "en")
        self.assertEqual([l["text"] for l in lines], ["Hi.", "Me too."])

    def test_long_sentence_without_punct_or_pause_not_cut_mid_sentence(self):
        # 回報案例:10 詞上限不再硬切成 "...live in" / "the States."。
        text = "car insurance is an absolute necessity if you live in the States."
        words = [_w(t, i * 0.3, i * 0.3 + 0.25) for i, t in enumerate(text.split())]
        lines = _split(
            [{"text": text, "start": 0.0, "end": 4.0, "words": words}],
            "en",
            max_words=10,
        )
        self.assertEqual([l["text"] for l in lines], [text])

    def test_long_line_split_at_clause_punct(self):
        words = [_w("a,", 0.0, 0.2), _w("b", 0.3, 0.5), _w("c", 0.6, 0.8), _w("d", 0.9, 1.0)]
        lines = _split([{"text": "", "start": 0.0, "end": 1.0, "words": words}], "en")
        self.assertEqual([l["text"] for l in lines], ["a,", "b c d"])

    def test_long_line_split_at_pause(self):
        # 4 詞 > 上限 3,停頓 0.8s(≥ 0.6、< 1.5)可作為斷點。
        words = [_w("a", 0.0, 0.2), _w("b", 0.3, 0.5), _w("c", 1.3, 1.5), _w("d", 1.6, 1.8)]
        lines = _split([{"text": "", "start": 0.0, "end": 2.0, "words": words}], "en")
        self.assertEqual([l["text"] for l in lines], ["a b", "c d"])

    def test_cjk_joined_without_space_and_counted_by_chars(self):
        words = [_w(ch, i * 0.2, i * 0.2 + 0.1) for i, ch in enumerate("我愛你中國人")]
        words.insert(3, _w("，"))
        lines = _split([{"text": "", "start": 0.0, "end": 2.0, "words": words}], "zh")
        self.assertEqual([l["text"] for l in lines], ["我愛你，", "中國人"])

    def test_script_switch_does_not_split(self):
        words = [_w(c, 0.1 * i, 0.1 * i + 0.05) for i, c in enumerate("Apple蘋果")]
        lines = _split(
            [{"text": "", "start": 0.0, "end": 1.0, "words": words}], "zh", max_chars_cjk=14
        )
        self.assertEqual([l["text"] for l in lines], ["Apple蘋果"])

    def test_mixed_script_word_stays_on_one_line(self):
        # 單一詞 "Absorb,吸收." 不在語系切換處斷:整行沿用該詞的起訖時間。
        words = [_w("Absorb,吸收.", 1.0, 2.0)]
        lines = _split(
            [{"text": "", "start": 1.0, "end": 2.0, "words": words}], "en", max_words=10
        )
        self.assertEqual([l["text"] for l in lines], ["Absorb,吸收."])
        self.assertAlmostEqual(lines[0]["start"], 1.0)
        self.assertAlmostEqual(lines[0]["end"], 2.0)

    def test_splits_on_max_duration_at_clause_punct(self):
        words = [_w("a,", 0.0, 2.0), _w("b,", 2.1, 4.0), _w("c", 4.1, 6.0)]
        lines = _split(
            [{"text": "", "start": 0.0, "end": 6.0, "words": words}], "en", max_words=10
        )
        self.assertEqual([l["text"] for l in lines], ["a, b,", "c"])

    def test_untimed_words_do_not_break_and_fallback_to_segment_times(self):
        # 數字等字典外字元無時間:不觸發停頓斷行,首 / 末行時間退回原段。
        words = [_w("1"), _w("2", 1.0, 1.5), _w("3")]
        lines = _split([{"text": "", "start": 0.5, "end": 2.0, "words": words}], "en")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "1 2 3")
        # 行首 "1" 無時間 → 從 "2"(1.0)往回推 1 單元(預設 0.3s)。
        self.assertAlmostEqual(lines[0]["start"], 0.7)
        self.assertEqual(lines[0]["end"], 1.5)

    def test_leading_digits_in_cjk_backfill_start(self):
        # 中文段 "520我":數字無時間 → 從「我」往回推 3 單元。
        words = [_w(c) for c in "520"] + [_w("我", 5.0, 5.5)]
        lines = _split(
            [{"text": "", "start": 5.0, "end": 5.5, "words": words}],
            "zh",
            max_chars_cjk=14,
            oov_unit_seconds=0.3,
        )
        self.assertEqual(lines[0]["text"], "520我")
        self.assertAlmostEqual(lines[0]["start"], 4.1)

    def test_backfill_not_before_previous_line_end(self):
        # 句號切出第二行「1你」:往回推不可早於第一行結束(4.95)。
        words = [
            _w("我", 0.0, 4.9), _w("好", 4.9, 4.95), _w("。"), _w("1"), _w("你", 5.0, 5.5),
        ]
        lines = _split(
            [{"text": "", "start": 0.0, "end": 5.5, "words": words}],
            "zh",
            oov_unit_seconds=0.3,
        )
        self.assertEqual([l["text"] for l in lines], ["我好。", "1你"])
        self.assertEqual(lines[1]["start"], 4.95)

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



def _c(ch, start=None, end=None):
    return {"char": ch, "start": start, "end": end}


class CharsToWordsTest(unittest.TestCase):
    def test_cjk_one_word_per_char_keeping_inner_spaces(self):
        chars = [_c(" "), _c("你", 0.0, 0.2), _c(" "), _c("好", 0.9, 1.1), _c("3"), _c(" ")]
        words = chars_to_words(" 你 好3 ", chars, "")
        self.assertEqual(
            words,
            [
                {"word": "你", "start": 0.0, "end": 0.2},
                {"word": " "},  # 字間空白保留(無時間);頭尾的不留
                {"word": "好", "start": 0.9, "end": 1.1},
                {"word": "3"},  # 字典外:無時間
            ],
        )

    def test_cjk_segment_keeps_spaces_between_latin_words(self):
        # 中文段夾英文:英文詞之間的空白要留著,不能黏成 "takeitorleaveit"。
        text = "讲价 Take it or leave it, I won't bargain."
        chars = [_c(ch) for ch in text]
        chars[0], chars[1] = _c("讲", 0.0, 0.3), _c("价", 0.3, 0.6)
        seg = {
            "text": text,
            "start": 0.0,
            "end": 5.0,
            "words": chars_to_words(text, chars, ""),
        }
        lines = split_segments_into_lines([seg], "zh")
        self.assertEqual("".join(l["text"] for l in lines), text)
        self.assertEqual(len(lines), 1)

    def test_spaced_language_groups_by_whitespace(self):
        text = "hi, you"
        chars = [
            _c("h", 0.0, 0.1), _c("i", 0.1, 0.2), _c(","),
            _c(" "),
            _c("y", 1.0, 1.1), _c("o", 1.1, 1.2), _c("u", 1.2, 1.3),
        ]
        words = chars_to_words(text, chars, " ")
        self.assertEqual(
            words,
            [
                {"word": "hi,", "start": 0.0, "end": 0.2},
                {"word": "you", "start": 1.0, "end": 1.3},
            ],
        )

    def test_gap_survives_into_line_split(self):
        # 字間停頓(blank 幀)保留下來 → 切行能在長停頓處斷開。
        chars = [_c("甲", 0.0, 0.3), _c("乙", 0.3, 0.6), _c("丙", 2.5, 2.8), _c("丁", 2.8, 3.1)]
        seg = {
            "text": "甲乙丙丁",
            "start": 0.0,
            "end": 3.1,
            "words": chars_to_words("甲乙丙丁", chars, ""),
        }
        lines = split_segments_into_lines([seg], "zh")
        self.assertEqual([l["text"] for l in lines], ["甲乙", "丙丁"])
        self.assertEqual(lines[1]["start"], 2.5)


class AlignmentSegmentsTest(unittest.TestCase):
    def test_copies_and_keeps_length_without_masking(self):
        segs = [{"text": "Ｈｉ (x2)", "start": 0.0, "end": 1.0}]
        out = _alignment_segments(segs, "en")
        self.assertEqual(out[0]["text"], "Hi (x2)")  # NFKC,不遮蔽
        self.assertEqual(len(out[0]["text"]), len(segs[0]["text"]))
        self.assertEqual(segs[0]["text"], "Ｈｉ (x2)")  # 原段不被修改


class FinalizeFragmentsTest(unittest.TestCase):
    def test_lead_applied_without_script(self):
        frags = [{"index": 0, "begin": 1.0, "end": 2.0, "text": "放弃"}]
        finalize_fragments(frags, None, lead_seconds=0.2)
        self.assertAlmostEqual(frags[0]["begin"], 0.8)
        self.assertEqual(frags[0]["text"], "放弃")  # 不轉換


class CollapseRepetitionsTest(unittest.TestCase):
    def test_filler_run_removed_entirely(self):
        self.assertEqual(collapse_repetitions("啊" * 90), "")

    def test_leading_filler_run_removed_from_lyric(self):
        self.assertEqual(
            collapse_repetitions("啊" * 30 + "為何臨水前會想要留一盞燈"),
            "為何臨水前會想要留一盞燈",
        )

    def test_short_repeat_kept(self):
        self.assertEqual(collapse_repetitions("斑斑斑"), "斑斑斑")
        self.assertEqual(collapse_repetitions("啊啊"), "啊啊")

    def test_non_filler_phrase_trimmed_to_max(self):
        self.assertEqual(collapse_repetitions("我愛你" * 6, max_repeats=3), "我愛你" * 3)

    def test_mid_line_cut_adds_no_space_in_cjk(self):
        self.assertEqual(collapse_repetitions("前面" + "啊" * 10 + "後面"), "前面後面")

    def test_latin_filler_words_removed(self):
        self.assertEqual(collapse_repetitions("la la la la la la hello"), "hello")

    def test_filler_with_punctuation_pattern(self):
        self.assertEqual(collapse_repetitions("啊，" * 8 + "我愛你"), "我愛你")

    def test_disabled_when_max_repeats_zero(self):
        self.assertEqual(collapse_repetitions("啊" * 10, max_repeats=0), "啊" * 10)


class DropHallucinatedSegmentsTest(unittest.TestCase):
    def test_drops_filler_only_and_cleans_mixed(self):
        segs = [
            _seg("啊" * 64, 5.0, 11.0),
            _seg("啊啊", 20.0, 20.7),
            _seg("啊" * 40 + "為何臨水前", 23.0, 37.0),
            _seg("要為自己保留幾分", 77.0, 80.0),
        ]
        out = drop_hallucinated_segments(segs)
        self.assertEqual([s["text"] for s in out], ["為何臨水前", "要為自己保留幾分"])
        self.assertEqual((out[0]["start"], out[0]["end"]), (23.0, 37.0))
        self.assertEqual(segs[2]["text"], "啊" * 40 + "為何臨水前")  # 不改輸入

    def test_drops_empty_and_punct_only(self):
        segs = [_seg("", 0.0, 1.0), _seg("……", 1.0, 2.0), _seg("hello", 2.0, 3.0)]
        self.assertEqual([s["text"] for s in drop_hallucinated_segments(segs)], ["hello"])


if __name__ == "__main__":
    unittest.main()
