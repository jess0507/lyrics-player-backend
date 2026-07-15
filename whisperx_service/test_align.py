"""字級對齊 → 逐行片段 的純邏輯單元測試。

只測 :func:`aligner.build_fragments` / :func:`aligner.normalize_language` /
:func:`aligner.separator_for` 等不依賴 whisperx / torch 的部分(aligner 對 whisperx
採延後 import,故這些函式可在無重依賴的環境下直接測試):

    python -m unittest test_align
"""

import unittest

from aligner import (
    _chunk_bounds,
    build_fragments,
    frame_spans,
    normalize_language,
    separator_for,
    tokenize_text,
)


def _char(ch, start=None, end=None):
    return {"char": ch, "start": start, "end": end}


class NormalizeLanguageTest(unittest.TestCase):
    def test_chinese_variants_map_to_zh(self):
        for code in ["zh", "zh-TW", "zh_CN", "cmn", "yue", "zh-Hant"]:
            self.assertEqual(normalize_language(code), "zh")

    def test_iso639_3_and_two_letter(self):
        self.assertEqual(normalize_language("eng"), "en")
        self.assertEqual(normalize_language("en"), "en")
        self.assertEqual(normalize_language("jpn"), "ja")

    def test_empty_defaults_en(self):
        self.assertEqual(normalize_language(""), "en")

    def test_unknown_takes_primary_subtag(self):
        self.assertEqual(normalize_language("sv-SE"), "sv")

    def test_separator(self):
        self.assertEqual(separator_for("zh"), "")
        self.assertEqual(separator_for("ja"), "")
        self.assertEqual(separator_for("en"), " ")


class BuildFragmentsTest(unittest.TestCase):
    def test_cjk_no_separator_maps_chars_to_lines(self):
        lines = ["ab", "cd"]
        chars = [
            _char("a", 0.0, 1.0),
            _char("b", 1.0, 2.0),
            _char("c", 2.0, 3.0),
            _char("d", 3.0, 4.0),
        ]
        frags, timed = build_fragments(lines, chars, "")
        self.assertEqual(timed, 2)
        self.assertEqual(frags[0]["begin"], 0.0)
        self.assertEqual(frags[0]["end"], 2.0)
        self.assertEqual(frags[1]["begin"], 2.0)
        self.assertEqual(frags[1]["end"], 4.0)
        self.assertEqual([f["text"] for f in frags], lines)

    def test_space_separator_accounts_for_gap_char(self):
        # "ab cd":索引 2 為分隔空格(無時間),不應吃掉任何一行的字。
        lines = ["ab", "cd"]
        chars = [
            _char("a", 0.0, 1.0),
            _char("b", 1.0, 2.0),
            _char(" "),
            _char("c", 5.0, 6.0),
            _char("d", 6.0, 7.0),
        ]
        frags, timed = build_fragments(lines, chars, " ")
        self.assertEqual(timed, 2)
        self.assertEqual(frags[0]["begin"], 0.0)
        self.assertEqual(frags[1]["begin"], 5.0)

    def test_line_without_timing_carries_forward(self):
        # 中間整行無時間 → 沿用前一行結束時間,保持單調遞增、不漏行。
        lines = ["ab", "cd", "ef"]
        chars = [
            _char("a", 0.0, 1.0),
            _char("b", 1.0, 2.0),
            _char("c"),
            _char("d"),
            _char("e", 5.0, 6.0),
            _char("f", 6.0, 7.0),
        ]
        frags, timed = build_fragments(lines, chars, "")
        self.assertEqual(timed, 2)  # 只有第 0、2 行有時間
        self.assertEqual(frags[1]["begin"], 2.0)  # 沿用前一行 end
        begins = [f["begin"] for f in frags]
        self.assertEqual(begins, sorted(begins))  # 單調遞增

    def test_length_mismatch_falls_back_to_proportional(self):
        # char 串流長度與串接文字不符 → 比例分配(仍與行一一對應、單調遞增)。
        lines = ["ab", "cd"]
        chars = [_char("x", 0.0, 4.0)]  # 長度 1 != 串接 "abcd" 長度 4
        frags, timed = build_fragments(lines, chars, "")
        self.assertEqual(len(frags), 2)
        self.assertEqual(timed, 2)
        self.assertEqual(frags[0]["begin"], 0.0)
        self.assertEqual(frags[1]["begin"], 2.0)  # 0 + 4 * (2/4)

    def test_no_timing_at_all_returns_zeroed(self):
        lines = ["ab"]
        chars = [_char("a"), _char("b")]
        frags, timed = build_fragments(lines, chars, "")
        # 全行無時間 → carry forward 為 0,covered 0(上層據覆蓋率判失敗)。
        self.assertEqual(timed, 0)
        self.assertEqual(frags[0]["begin"], 0.0)


class TokenizeTextTest(unittest.TestCase):
    # 仿 whisperx 字典:blank(<pad>)=0、詞界 "|"=1、小寫字母。
    DICT = {"<pad>": 0, "|": 1, "a": 2, "b": 3, "c": 4}

    def test_maps_known_chars_and_records_positions(self):
        tokens, idx = tokenize_text("ab!c", self.DICT, 0)
        self.assertEqual(tokens, [2, 3, 4])
        self.assertEqual(idx, [0, 1, 3])  # "!" 不在字典 → 略過但不佔位

    def test_space_maps_to_word_delimiter(self):
        tokens, idx = tokenize_text("a b", self.DICT, 0)
        self.assertEqual(tokens, [2, 1, 3])
        self.assertEqual(idx, [0, 1, 2])

    def test_uppercase_matches_lowercased_dictionary(self):
        tokens, _ = tokenize_text("AB", self.DICT, 0)
        self.assertEqual(tokens, [2, 3])

    def test_blank_token_never_emitted(self):
        # 字典若把某字元映到 blank id,不得混入目標序列。
        tokens, _ = tokenize_text("a-b", {**self.DICT, "-": 0}, 0)
        self.assertEqual(tokens, [2, 3])


class FrameSpansTest(unittest.TestCase):
    def test_merges_consecutive_and_skips_blank(self):
        # 路徑:a a - b - - c c(blank=0)
        spans = frame_spans([2, 2, 0, 3, 0, 0, 4, 4], 0)
        self.assertEqual(spans, [(0, 1), (3, 3), (6, 7)])

    def test_repeated_token_separated_by_blank(self):
        # 相同 token 連續出現兩次 → CTC 路徑以 blank 相隔 → 兩個區間。
        spans = frame_spans([2, 0, 2], 0)
        self.assertEqual(spans, [(0, 0), (2, 2)])

    def test_adjacent_different_tokens_without_blank(self):
        spans = frame_spans([2, 3], 0)
        self.assertEqual(spans, [(0, 0), (1, 1)])

    def test_trailing_span_closed(self):
        spans = frame_spans([0, 2, 2], 0)
        self.assertEqual(spans, [(1, 2)])


class ChunkBoundsTest(unittest.TestCase):
    # 視窗 16000 樣本 = 1 秒;尾塊 < 1 秒(_SAMPLE_RATE)時併入前一塊。
    def test_exact_multiple(self):
        self.assertEqual(_chunk_bounds(32000, 16000), [0, 16000, 32000])

    def test_short_tail_merged_into_previous(self):
        self.assertEqual(_chunk_bounds(33000, 16000), [0, 16000, 33000])

    def test_long_tail_kept(self):
        self.assertEqual(
            _chunk_bounds(65000, 16000), [0, 16000, 32000, 48000, 65000]
        )

    def test_single_short_chunk(self):
        self.assertEqual(_chunk_bounds(8000, 16000), [0, 8000])


if __name__ == "__main__":
    unittest.main()
