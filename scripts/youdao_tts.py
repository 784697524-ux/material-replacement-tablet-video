#!/usr/bin/env python3
"""
youdao-tts: 有道 confucius4-tts Gradio 接口的 CLI 封装。

**实现方式**：直接调 Gradio HTTP + SSE (protocol sse_v3)，不用 gradio_client
（gradio_client 1.x/2.x 都跟服务端 Gradio 4.44.1 的 api_info schema 不兼容，
会报 "argument of type 'bool' is not iterable" 或 "FileData() argument after ** must be a mapping"）。

服务端接口（2026-09-10 探测）：
    fn_index=0  api_name=_gradio_reference_uploaded
        inputs:  [audio]
        outputs: [state, textbox]
    fn_index=1  api_name=_gradio_predict
        inputs:  [textbox, dropdown, audio, state]
        outputs: [audio, textbox]

流程：
    1. 上传参考音频 → 拿到服务端文件 token
    2. 调 fn_index=0 → 拿到 state
    3. 调 fn_index=1（text, lang, file, state）→ 拿到 output audio token + status
    4. 下载 output audio → 落到目标 wav 路径

用法：
    python tts.py --text "你好"                                # 用默认参考音频克隆
    python tts.py --text "hello" --lang en --ref /path/ref.wav
    python tts.py --text x --set-default-ref /path/ref.wav     # 只写配置
    python tts.py --text x --show-config

stdout 最后一行必定是 wav 绝对路径。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

CONFIG_PATH = Path.home() / ".youdao-tts" / "config.json"
DEFAULT_ENDPOINT = "https://confucius4-tts.youdao.com/gradio/"
DEFAULT_OUTPUT_ROOT = Path.cwd() / "outputs" / "tts"
SUPPORTED_LANGS = ["zh", "en", "ja", "ko", "de", "fr", "th", "id", "vi",
                   "es", "pt", "it", "ru", "ms"]

FN_UPLOAD_REF = 0     # _gradio_reference_uploaded
FN_PREDICT = 1        # _gradio_predict
SSE_TIMEOUT = 600.0   # 单次合成上限 10 分钟


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"[warn] 解析 {CONFIG_PATH} 失败: {e}")
    return {}


def normalize_endpoint(ep: str) -> str:
    return ep.rstrip("/")


def resolve_ref(cli_ref, cfg) -> str:
    ref = cli_ref or cfg.get("reference_audio")
    if not ref:
        raise SystemExit(
            f"[error] 未提供参考音频。请用 --ref 指定，或写入 {CONFIG_PATH} 的 "
            "reference_audio 字段。"
        )
    return ref


def resolve_out(cli_out, name) -> Path:
    if cli_out:
        out = Path(cli_out).expanduser().resolve()
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = f"{ts}-{name}" if name else ts
        out = DEFAULT_OUTPUT_ROOT / f"{stem}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def materialize_ref(ref: str, workdir: Path) -> Path:
    """把参考音频落到本地临时路径（如果是 URL 就下载）。"""
    if is_url(ref):
        workdir.mkdir(parents=True, exist_ok=True)
        name = Path(urlparse(ref).path).name or "ref_audio"
        local = workdir / name
        with httpx.Client(timeout=60.0, follow_redirects=True) as c:
            r = c.get(ref)
            r.raise_for_status()
            local.write_bytes(r.content)
        return local
    p = Path(ref).expanduser().resolve()
    if not p.exists():
        raise SystemExit(f"[error] 参考音频不存在: {p}")
    return p


def upload_file(client: httpx.Client, root: str, session_hash: str,
                local: Path) -> dict:
    """POST /upload?upload_id=<session_hash>，返回 FileData dict。"""
    url = f"{root}/upload"
    mime = mimetypes.guess_type(str(local))[0] or "application/octet-stream"
    with local.open("rb") as fh:
        files = {"files": (local.name, fh, mime)}
        r = client.post(url, params={"upload_id": session_hash}, files=files)
    r.raise_for_status()
    payload = r.json()
    # 期望 ["/tmp/gradio/.../xxx.wav"]
    if not isinstance(payload, list) or not payload:
        raise SystemExit(f"[error] /upload 返回异常: {payload!r}")
    server_path = payload[0]
    return {
        "path": server_path,
        "orig_name": local.name,
        "size": local.stat().st_size,
        "mime_type": mime,
        "meta": {"_type": "gradio.FileData"},
    }


def sse_wait_completed(client: httpx.Client, root: str, session_hash: str,
                       event_id: str) -> dict:
    """GET /queue/data (SSE)，等到 msg=process_completed 事件返回其 JSON。

    服务端不使用 `event:` 前缀行，事件类型藏在 data 里的 "msg" 字段。
    """
    url = f"{root}/queue/data"
    params = {"session_hash": session_hash}
    if event_id:
        params["event_id"] = event_id

    with client.stream("GET", url, params=params, timeout=SSE_TIMEOUT) as r:
        r.raise_for_status()
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            if not line.startswith("data:"):
                continue
            body = line[5:].lstrip()
            try:
                msg = json.loads(body)
            except Exception:
                continue
            mtype = msg.get("msg")
            if mtype == "process_completed":
                return msg
            if mtype == "unexpected_error":
                raise SystemExit(f"[error] SSE 意外错误: {msg}")
            if mtype == "close_stream":
                raise SystemExit(f"[error] SSE 流在 process_completed 前关闭: {msg}")
    raise SystemExit("[error] SSE 流结束但未收到 process_completed")


def queue_join(client: httpx.Client, root: str, session_hash: str,
               fn_index: int, data: list) -> str:
    """POST /queue/join → 返回 event_id。"""
    url = f"{root}/queue/join"
    body = {
        "data": data,
        "fn_index": fn_index,
        "session_hash": session_hash,
    }
    r = client.post(url, json=body, timeout=60.0)
    r.raise_for_status()
    j = r.json()
    return str(j.get("event_id") or "")


def call_fn(client: httpx.Client, root: str, session_hash: str,
            fn_index: int, data: list) -> list:
    event_id = queue_join(client, root, session_hash, fn_index, data)
    msg = sse_wait_completed(client, root, session_hash, event_id)
    if msg.get("success") is False:
        raise SystemExit(f"[error] fn_index={fn_index} 失败: {msg}")
    return msg.get("output", {}).get("data", [])


def download_output(client: httpx.Client, root: str, file_data: dict,
                    dest: Path) -> None:
    """从服务端下载 FileData 指向的音频文件。"""
    if not isinstance(file_data, dict):
        raise SystemExit(f"[error] output FileData 异常: {file_data!r}")
    path = file_data.get("path") or file_data.get("url")
    if not path:
        raise SystemExit(f"[error] output FileData 缺 path: {file_data!r}")
    if is_url(path):
        url = path
    else:
        # Gradio 4.x: /file=<server_path>
        url = f"{root}/file={path}"
    r = client.get(url, timeout=120.0, follow_redirects=True)
    r.raise_for_status()
    dest.write_bytes(r.content)


def synthesize(endpoint: str, text: str, lang: str, ref_local: Path) -> tuple[bytes, str]:
    """端到端跑一次合成，返回 (wav_bytes, status_str)。"""
    root = normalize_endpoint(endpoint)
    session_hash = uuid.uuid4().hex[:16]

    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        log(f"[info] session_hash={session_hash}")
        log(f"[info] uploading ref: {ref_local} ({ref_local.stat().st_size} bytes)")
        file_data = upload_file(client, root, session_hash, ref_local)
        log(f"[info] uploaded server_path={file_data['path']}")

        log("[info] step1: _gradio_reference_uploaded (fn_index=0)")
        t0 = time.time()
        up_out = call_fn(client, root, session_hash, FN_UPLOAD_REF, [file_data])
        log(f"[info] step1 done in {time.time()-t0:.2f}s, output_len={len(up_out)}")
        # up_out = [state, status_text]
        state = up_out[0] if len(up_out) >= 1 else None
        up_status = up_out[1] if len(up_out) >= 2 else ""
        log(f"[info] step1 status={up_status!r}")

        log("[info] step2: _gradio_predict (fn_index=1)")
        t1 = time.time()
        pred_out = call_fn(client, root, session_hash, FN_PREDICT,
                            [text, lang, file_data, state])
        log(f"[info] step2 done in {time.time()-t1:.2f}s, output_len={len(pred_out)}")
        if len(pred_out) < 2:
            raise SystemExit(f"[error] predict 返回不足 2 项: {pred_out!r}")
        out_file_data, out_status = pred_out[0], pred_out[1]
        log(f"[info] step2 status={out_status!r}")
        log(f"[info] downloading output: {out_file_data}")

        # 直接下载到临时 bytes
        path = (out_file_data or {}).get("path") if isinstance(out_file_data, dict) else None
        if not path:
            raise SystemExit(f"[error] output FileData 缺 path: {out_file_data!r}")
        url = path if is_url(path) else f"{root}/file={path}"
        r = client.get(url, timeout=120.0, follow_redirects=True)
        r.raise_for_status()
        return r.content, str(out_status)


def prepare_ref(src: Path, start: float, dur: float, out: Path) -> Path:
    """从 mp4/mp3/m4a/wav 等文件里抽一段人声做参考音频。

    输出 mono 24kHz pcm_s16le wav（服务端最稳的格式）。
    """
    import shutil as _sh
    import subprocess
    if _sh.which("ffmpeg") is None:
        raise SystemExit("[error] 未找到 ffmpeg。请先安装 ffmpeg（brew install ffmpeg 或官网下载）。")
    src = Path(src).expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"[error] 源文件不存在: {src}")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start}", "-i", str(src),
           "-t", f"{dur}", "-vn", "-ac", "1", "-ar", "24000",
           "-c:a", "pcm_s16le", str(out)]
    log(f"[info] 抽取参考音频: {src} [{start}s, +{dur}s] -> {out}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"[error] ffmpeg 抽音频失败: {r.stderr[-500:]}")
    if not out.exists() or out.stat().st_size < 20000:
        raise SystemExit(
            f"[error] 抽出的 wav 太小（{out.stat().st_size if out.exists() else 0} bytes），"
            "该时间段可能没有人声，请用 --ref-start 换一段。")
    return out


def main():
    ap = argparse.ArgumentParser(description="Youdao confucius4-tts CLI (direct HTTP)")
    ap.add_argument("--text", default=None, help="待合成文本（合成时必填）")
    ap.add_argument("--lang", default=None, choices=SUPPORTED_LANGS,
                    help="语言，默认读 config 或 zh")
    ap.add_argument("--ref", default=None,
                    help="参考音频本地路径或 URL；不填读 config.reference_audio")
    ap.add_argument("--out", default=None, help="输出 wav 绝对路径")
    ap.add_argument("--name", default=None, help="输出文件名 slug")
    ap.add_argument("--endpoint", default=None, help="Gradio endpoint URL")
    ap.add_argument("--prepare-ref", default=None, metavar="MEDIA_FILE",
                    help="从 mp4/mp3/m4a/wav 抽一段人声做参考音频 wav 后退出；"
                         "配合 --ref-start/--ref-dur 选段，配合 --set-default 直接设为默认音色")
    ap.add_argument("--ref-start", type=float, default=0.0,
                    help="--prepare-ref 抽音频的起始秒数，默认 0")
    ap.add_argument("--ref-dur", type=float, default=10.0,
                    help="--prepare-ref 抽音频的时长秒数，默认 10（建议 3-10）")
    ap.add_argument("--set-default", action="store_true",
                    help="--prepare-ref 时把抽出的 wav 写入 config.reference_audio")
    ap.add_argument("--set-default-ref", default=None, metavar="PATH_OR_URL",
                    help="写入 config.reference_audio 后退出")
    ap.add_argument("--show-config", action="store_true", help="打印当前配置")
    ap.add_argument("--probe", action="store_true",
                    help="只探测 /config 打印服务端信息后退出")
    args = ap.parse_args()

    cfg = load_config()
    endpoint = args.endpoint or cfg.get("endpoint") or DEFAULT_ENDPOINT

    if args.set_default_ref:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cfg["reference_audio"] = args.set_default_ref
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        log(f"[ok] 已写入 {CONFIG_PATH}: {json.dumps(cfg, ensure_ascii=False)}")
        return 0

    if args.show_config:
        print(json.dumps({"config_path": str(CONFIG_PATH), "endpoint": endpoint, **cfg},
                          ensure_ascii=False, indent=2))
        return 0

    if args.probe:
        root = normalize_endpoint(endpoint)
        with httpx.Client(timeout=30.0) as c:
            r = c.get(f"{root}/config")
            r.raise_for_status()
            j = r.json()
        print(json.dumps({
            "version": j.get("version"),
            "protocol": j.get("protocol"),
            "mode": j.get("mode"),
            "enable_queue": j.get("enable_queue"),
            "dependencies": [
                {"id": d.get("id"), "api_name": d.get("api_name"),
                 "queue": d.get("queue"),
                 "inputs": len(d.get("inputs", [])),
                 "outputs": len(d.get("outputs", []))}
                for d in j.get("dependencies", [])
            ],
        }, ensure_ascii=False, indent=2))
        return 0

    if args.prepare_ref:
        refs_root = Path.home() / ".youdao-tts" / "refs"
        if args.out:
            ref_out = Path(args.out).expanduser().resolve()
        else:
            stem = Path(args.prepare_ref).stem
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            ref_out = refs_root / f"{stem}-{ts}.wav"
        prepare_ref(Path(args.prepare_ref), args.ref_start, args.ref_dur, ref_out)
        log(f"[ok] 参考音频已抽出: {ref_out} "
            f"({ref_out.stat().st_size} bytes, {args.ref_dur}s)")
        if args.set_default:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            cfg["reference_audio"] = str(ref_out)
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
            log(f"[ok] 已设为默认音色: {CONFIG_PATH}")
        print(str(ref_out))
        return 0

    if not args.text:
        raise SystemExit("[error] 合成需要 --text；抽参考音频用 --prepare-ref <文件>。")

    lang = args.lang or cfg.get("language") or "zh"
    ref = resolve_ref(args.ref, cfg)
    out = resolve_out(args.out, args.name)

    # 参考音频若是 URL 则下载到临时目录
    tmp_root = Path.home() / ".youdao-tts" / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    ref_local = materialize_ref(ref, tmp_root)

    t0 = time.time()
    log(f"[info] endpoint={endpoint}")
    log(f"[info] lang={lang}  text_len={len(args.text)}")
    wav_bytes, status = synthesize(endpoint, args.text, lang, ref_local)
    out.write_bytes(wav_bytes)
    dt = time.time() - t0
    log(f"[info] status={status}")
    log(f"[info] elapsed={dt:.2f}s  size={out.stat().st_size} bytes  out={out}")
    print(str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
