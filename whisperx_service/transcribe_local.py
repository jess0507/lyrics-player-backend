"""本機測試:用一個音訊檔(.mp3 等)直接跑 WhisperX 自動產生歌詞。

不經 HTTP / GCS,直接呼叫 :func:`transcriber.transcribe`,印出產生的 LRC 並可寫檔。
用來在本機快速驗證 ASR 轉寫品質(尤其中文 / 歌聲),毋須建容器或部署。

需求(本機需先裝好重依賴):
    pip install --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements.txt
    # 另需系統有 ffmpeg(whisperx.load_audio 以 ffmpeg 解碼)。

用法:
    python transcribe_local.py song.mp3                    # 自動偵測語言
    python transcribe_local.py song.mp3 -l zh-TW           # 指定語言鎖定
    python transcribe_local.py song.mp3 -o out.lrc         # 同時寫出 .lrc
    WHISPER_MODEL_SIZE=medium python transcribe_local.py song.mp3  # 換模型大小

退出碼:0 成功;2 轉寫失敗(辨識不出歌詞);1 其他錯誤。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from transcriber import TranscriptionError, transcribe


def main() -> int:
    parser = argparse.ArgumentParser(
        description="WhisperX 本機自動產生歌詞測試:音訊 → LRC",
    )
    parser.add_argument("audio", help="音訊檔路徑(.mp3 / .m4a / .wav …,ffmpeg 可解)")
    parser.add_argument(
        "-l",
        "--language",
        default=None,
        help="語言碼(BCP-47 / 二字母 / ISO 639-3);省略則自動偵測",
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

    print(
        f"音訊:{args.audio}\n"
        f"語言:{args.language or '自動偵測'}\n"
        f"模型:{os.environ.get('WHISPER_MODEL_SIZE', 'small')}\n"
        f"轉寫中(首次會下載 ASR / 對齊模型,請稍候)…",
        file=sys.stderr,
    )

    try:
        result = transcribe(args.audio, args.language)
    except TranscriptionError as exc:
        # 轉寫失敗 = 辨識不出可用歌詞(對應後端 422 transcription_failed)。
        print(f"轉寫失敗:{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 其他非預期錯誤
        print(f"錯誤:{exc}", file=sys.stderr)
        return 1

    print(f"偵測語言:{result['language']}", file=sys.stderr)
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
