"""本機測試:用一個音訊檔(.mp3 等)+ 一個歌詞 .txt 檔,直接跑 WhisperX 對齊。

不經 HTTP / GCS,直接呼叫 :func:`aligner.align`,印出對齊後的 LRC 並可寫檔。
用來在本機快速驗證對齊品質(尤其中文 / 歌聲),毋須建容器或部署。

需求(本機需先裝好重依賴):
    pip install --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements.txt
    # 另需系統有 ffmpeg(whisperx.load_audio 以 ffmpeg 解碼)。

用法:
    python align_local.py song.mp3 lyrics.txt                 # 自動偵測 → 預設 en
    python align_local.py song.mp3 lyrics.txt -l zh-TW        # 指定語言
    python align_local.py song.mp3 lyrics.txt -l zh -o out.lrc  # 同時寫出 .lrc

語言碼可用 BCP-47 / 二字母 / ISO 639-3(會正規化為 whisperx 二字母碼):
    中文 zh / zh-TW / cmn …、日文 ja、英文 en / eng …(見 aligner._LANGUAGE_MAP)。

退出碼:0 成功;2 對齊失敗(降級,應保留原 unsynced 文字);1 其他錯誤。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from aligner import AlignmentError, align, normalize_language


def _read_lines(txt_path: str) -> list[str]:
    """讀歌詞 .txt;保留原始行(空行交給 aligner 內部清理),處理 BOM。"""
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        return f.read().splitlines()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="WhisperX 本機對齊測試:音訊 + 歌詞 txt → LRC",
    )
    parser.add_argument("audio", help="音訊檔路徑(.mp3 / .m4a / .wav …,ffmpeg 可解)")
    parser.add_argument("lyrics", help="歌詞純文字檔路徑(.txt,每行一句)")
    parser.add_argument(
        "-l",
        "--language",
        default="en",
        help="語言碼(BCP-47 / 二字母 / ISO 639-3),預設 en;中文用 zh / zh-TW",
    )
    parser.add_argument(
        "-o",
        "--out",
        help="輸出 LRC 檔路徑(省略則只印到 stdout)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="印出逐行片段(begin/end 秒)與診斷資訊",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    if not os.path.exists(args.audio):
        print(f"找不到音訊檔:{args.audio}", file=sys.stderr)
        return 1
    if not os.path.exists(args.lyrics):
        print(f"找不到歌詞檔:{args.lyrics}", file=sys.stderr)
        return 1

    lines = _read_lines(args.lyrics)
    if not any(s.strip() for s in lines):
        print("歌詞檔為空(去空行後無內容)", file=sys.stderr)
        return 1

    print(
        f"音訊:{args.audio}\n"
        f"歌詞:{args.lyrics}({sum(1 for s in lines if s.strip())} 行非空)\n"
        f"語言:{args.language} → {normalize_language(args.language)}\n"
        f"對齊中(首次會下載 wav2vec2 模型,請稍候)…",
        file=sys.stderr,
    )

    try:
        result = align(lines, args.audio, args.language)
    except AlignmentError as exc:
        # 對齊失敗 = 應降級保留原 unsynced 文字(對應後端 422 alignment_failed)。
        print(f"對齊失敗(降級):{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 其他非預期錯誤
        print(f"錯誤:{exc}", file=sys.stderr)
        return 1

    if args.verbose:
        print("\n逐行片段(秒):", file=sys.stderr)
        for frag in result["fragments"]:
            print(
                f"  [{frag['index']:>3}] "
                f"{frag['begin']:7.2f} ~ {frag['end']:7.2f}  {frag['text']}",
                file=sys.stderr,
            )

    lrc = result["lrc"]
    print(lrc)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(lrc + "\n")
        print(f"\n已寫出:{args.out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
