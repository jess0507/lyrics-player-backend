"""對 localhost 上的 whisperx_service 做 HTTP 煙霧測試(smoke test)。

打真正的 HTTP 端點(`/healthz`、`/align`、`/transcribe`),用本機音訊檔以
``audio.inlineBase64`` 內嵌送出 —— 不需 GCS 憑證,適合本機 / 容器內驗證。

只用 Python 標準庫(urllib / json / base64),不需安裝任何套件,因此可直接用
系統 python3 執行,毋須進容器(服務本身才需要在容器內跑)。

先讓服務跑起來(見 README「本機驗證」,用 Docker):
    cd whisperx_service
    docker build -t whisperx-service .
    docker run --rm -p 8080:8080 whisperx-service

再執行本 script:
    # 健康檢查
    python3 api_smoke.py --endpoint health

    # 自動產生歌詞(ASR):音訊 → LRC
    python3 api_smoke.py --audio song.mp3 --endpoint transcribe

    # 對齊既有歌詞:音訊 + 歌詞 txt → LRC
    python3 api_smoke.py --audio song.mp3 --lyrics lyrics.txt -l zh-TW --endpoint align

    # 一次跑全部(healthz + transcribe;有 --lyrics 時連 align)
    python3 api_smoke.py --audio song.mp3 --lyrics lyrics.txt -l zh-TW

退出碼:0 全部通過;1 有任一檢查失敗 / 連線失敗。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

# 與服務端 _MAX_AUDIO_BYTES 一致;inlineBase64 僅供小檔測試。
_MAX_AUDIO_BYTES = 50 * 1024 * 1024


def _post(url: str, payload: dict, timeout: float) -> tuple[int, dict]:
    """送 POST JSON,回 (status, body_dict)。非 2xx 也照樣解析 body。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse_json(resp.read())
    except urllib.error.HTTPError as exc:
        # 服務的錯誤合約把 code/message 放在 body,需讀出來才有意義。
        return exc.code, _parse_json(exc.read())


def _get(url: str, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse_json(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse_json(exc.read())


def _parse_json(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"_raw": raw[:500].decode("utf-8", "replace")}


def _audio_payload(audio_path: str) -> dict:
    """把本機音訊讀成 ``audio.inlineBase64`` 物件。"""
    size = os.path.getsize(audio_path)
    if size > _MAX_AUDIO_BYTES:
        raise SystemExit(
            f"音訊 {size/1024/1024:.1f} MB 超過 inlineBase64 上限 50 MB,請改用較短片段。"
        )
    with open(audio_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    fmt = os.path.splitext(audio_path)[1].lstrip(".") or "bin"
    return {"inlineBase64": b64, "format": fmt}


def _read_lines(txt_path: str) -> list[str]:
    """讀歌詞 txt(處理 BOM);空行交給服務端清理。"""
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        return f.read().splitlines()


def _show(title: str, status: int, body: dict, ok: bool) -> None:
    mark = "✅" if ok else "❌"
    print(f"\n{mark} {title} — HTTP {status}")
    # LRC 很長,單獨換行印;其餘 body 用縮排 JSON。
    lrc = body.pop("lrc", None) if isinstance(body, dict) else None
    print(json.dumps(body, ensure_ascii=False, indent=2))
    if lrc:
        print("--- lrc ---")
        print(lrc)


def check_health(base: str, timeout: float) -> bool:
    status, body = _get(f"{base}/healthz", timeout)
    ok = status == 200 and body.get("status") == "ok"
    _show("GET /healthz", status, body, ok)
    return ok


def check_transcribe(base: str, audio_path: str, language: str | None, timeout: float) -> bool:
    payload: dict = {"audio": _audio_payload(audio_path)}
    if language:
        payload["language"] = language
    status, body = _post(f"{base}/transcribe", payload, timeout)
    ok = status == 200 and bool(body.get("lrc"))
    _show("POST /transcribe", status, body, ok)
    return ok


def check_align(
    base: str, audio_path: str, lyrics_path: str, language: str | None, timeout: float
) -> bool:
    payload: dict = {
        "lines": _read_lines(lyrics_path),
        "audio": _audio_payload(audio_path),
    }
    if language:
        payload["language"] = language
    status, body = _post(f"{base}/align", payload, timeout)
    ok = status == 200 and bool(body.get("lrc"))
    _show("POST /align", status, body, ok)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description="whisperx_service localhost HTTP 煙霧測試(inlineBase64)。"
    )
    parser.add_argument("--url", default="http://localhost:8080", help="服務 base URL")
    parser.add_argument("--audio", help="本機音訊檔(.mp3/.m4a/.wav…),align/transcribe 需要")
    parser.add_argument("--lyrics", help="歌詞 txt(每行一句);提供則測 /align")
    parser.add_argument("-l", "--language", help="語言碼(如 zh-TW / ja / en);transcribe 可省略")
    parser.add_argument(
        "--endpoint",
        choices=["health", "transcribe", "align", "all"],
        default="all",
        help="要測哪個端點(預設 all)",
    )
    parser.add_argument(
        "--timeout", type=float, default=600.0, help="單一請求逾時秒數(CPU 對齊較久)"
    )
    args = parser.parse_args()

    base = args.url.rstrip("/")
    results: list[tuple[str, bool]] = []

    want = args.endpoint
    if want in ("all", "health"):
        results.append(("healthz", check_health(base, args.timeout)))

    if want in ("all", "transcribe"):
        if not args.audio:
            print("\n⚠️  略過 /transcribe:未提供 --audio")
        else:
            results.append(
                ("transcribe", check_transcribe(base, args.audio, args.language, args.timeout))
            )

    if want == "align" or (want == "all" and args.lyrics):
        if not args.audio or not args.lyrics:
            print("\n⚠️  略過 /align:需同時提供 --audio 與 --lyrics")
        else:
            results.append(
                ("align", check_align(base, args.audio, args.lyrics, args.language, args.timeout))
            )

    print("\n==== 結果 ====")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if results and all(ok for _, ok in results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as exc:
        print(f"\n❌ 連線失敗:{exc}")
        print("   服務有起來嗎?先:docker run --rm -p 8080:8080 whisperx-service")
        print("   或用 --url 指定其他位址。")
        sys.exit(1)
