# server.py —— NewCut 本地分析服务入口
#
# 职责：HTTP API（127.0.0.1），分析请求后台线程执行，支持
#   /analyze          发起分析（后台运行，可取消）
#   /job/{id}         查询进度与结果
#   /job/{id}/cancel  取消分析
#   /rebuild          基于缓存上下文重算方案（逐段排除/碎片合并/转场开关），免重解码
#   /thumb            取某秒帧的缩略图（JPEG base64，带锁复用 VideoCapture）
#   /heartbeat        面板心跳（超过 idle-exit 秒无心跳自动退出）
#   /shutdown         面板主动关停
#
# 启动（在 service/ 目录下）：
#   uv run python -m newcut_service.server --port 8765

import os
import sys

# 引擎模块使用扁平 import，把包目录加入 sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import argparse
import base64
import json
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

SERVICE_NAME = "newcut-service"
SERVICE_VERSION = "0.4.0"

# 引擎内部按相对路径读取模板目录，必须先切到模板所在目录
os.chdir(_HERE)

_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
_THUMB_CACHE_LIMIT = 240

_HEARTBEAT = {"last": time.time(), "idle_exit": 0.0}


def _watchdog():
    while True:
        time.sleep(5)
        if _HEARTBEAT["idle_exit"] > 0 and time.time() - _HEARTBEAT["last"] > _HEARTBEAT["idle_exit"]:
            print(f"[{SERVICE_NAME}] 超过 {_HEARTBEAT['idle_exit']:.0f}s 无心跳，自动退出", flush=True)
            os._exit(0)


def _setup_logging():
    """stdout/stderr 双写：落盘插件根目录 newcut-service.log + 保留原流。

    pythonw + stdio:ignore 下原流是 NUL 设备（不是 None），之前只在
    None 时重定向导致日志文件 0 字节。Tee 保证日志总有完整内容（含
    分析报错堆栈），同时不影响冒烟测试通过管道读取 "listening"。
    """
    path = os.path.join(os.path.dirname(_HERE), "newcut-service.log")
    try:
        f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return

    class _Tee:
        def __init__(self, orig):
            self.orig = orig

        def write(self, s):
            f.write(s)
            if self.orig is not None:
                try:
                    self.orig.write(s)
                except Exception:
                    pass
            return len(s)

        def flush(self):
            try:
                f.flush()
            except Exception:
                pass
            if self.orig is not None:
                try:
                    self.orig.flush()
                except Exception:
                    pass

        def fileno(self):
            return f.fileno()

        def isatty(self):
            return False

    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)
    print(f"\n[{SERVICE_NAME}] --- 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"v{SERVICE_VERSION} ---", flush=True)
    import decode
    decode.LOG_SINK = f  # 解码层 ffmpeg 异常退出的 stderr 也写进同一份日志


def _new_job(video_path: str, params: dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "id": job_id, "state": "running", "phase": "queued", "pct": 0.0,
            "detail": "", "video_path": video_path,
            "result": None, "error": None, "created": time.time(),
            "cancel": False, "context": None, "cap": None,
            "cap_lock": threading.Lock(), "thumbs": {},
        }
    return job_id


def _run_job(job_id: str, video_path: str, params: dict):
    job = _JOBS[job_id]
    t0 = time.time()

    def cb(phase, frac, detail=""):
        job["phase"], job["pct"], job["detail"] = phase, float(frac), detail
        if frac > 0:
            job["eta_sec"] = round((time.time() - t0) * (1 - frac) / frac, 1)

    def cancel_cb():
        return job.get("cancel")

    try:
        import pipeline
        result, context = pipeline.run_analysis_full(video_path, params, cb, cancel_cb)
        job["result"] = result
        job["context"] = context
        job["state"] = "done"
        job["pct"] = 1.0
        job["detail"] = "完成"
    except pipeline.Cancelled:
        job["state"] = "cancelled"
        job["detail"] = "已取消"
    except Exception as e:
        job["state"] = "error"
        job["error"] = f"{e}\n{traceback.format_exc()}"
        print(f"[job {job_id}] 分析失败: {job['error']}", flush=True)


def _get_thumb(job, sec: float, width: int = 192) -> str:
    """取指定秒的 JPEG 缩略图（base64 data URL），带缓存与线程锁。"""
    key = f"{width}:{round(sec, 2)}"
    if key in job["thumbs"]:
        return job["thumbs"][key]
    with job["cap_lock"]:
        if job["cap"] is None:
            cap = cv2.VideoCapture(job["video_path"])
            if not cap.isOpened():
                raise ValueError("无法打开视频（缩略图）")
            job["cap"] = cap
        cap = job["cap"]
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(sec * fps))
        ok, frame = cap.read()
    if not ok:
        raise ValueError(f"无法读取 {sec}s 处帧")
    h, w = frame.shape[:2]
    nh = max(1, int(width * h / max(1, w)))
    small = cv2.resize(frame, (width, nh), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 72])
    if not ok:
        raise ValueError("JPEG 编码失败")
    url = "data:image/jpeg;base64," + base64.b64encode(buf).decode("ascii")
    if len(job["thumbs"]) > _THUMB_CACHE_LIMIT:
        job["thumbs"].clear()
    job["thumbs"][key] = url
    return url


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
            self._send_json({
                "ok": True, "service": SERVICE_NAME, "version": SERVICE_VERSION,
                "python": sys.version.split()[0],
            })
        elif self.path.startswith("/job/"):
            job_id = self.path.rsplit("/", 1)[-1]
            job = _JOBS.get(job_id)
            if job is None:
                self._send_json({"error": "job not found"}, 404)
            else:
                out = {k: v for k, v in job.items()
                       if k not in ("context", "cap", "cap_lock", "thumbs", "cancel")}
                out["job_id"] = job_id
                self._send_json(out)
        elif self.path.startswith("/thumb"):
            try:
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                job = _JOBS.get((q.get("job_id") or [""])[0])
                if job is None or job.get("context") is None:
                    self._send_json({"error": "job 不存在或未完成"}, 404)
                    return
                sec = float((q.get("sec") or ["0"])[0])
                width = int((q.get("w") or ["192"])[0])
                self._send_json({"url": _get_thumb(job, sec, width)})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            if self.path == "/heartbeat":
                _HEARTBEAT["last"] = time.time()
                self._send_json({"ok": True})
            elif self.path == "/shutdown":
                self._send_json({"ok": True})
                threading.Timer(0.3, os._exit, args=(0,)).start()
            elif self.path == "/probe":
                body = self._read_json_body()
                import pipeline
                self._send_json(pipeline.probe_video(body.get("video_path", "")))
            elif self.path == "/analyze":
                body = self._read_json_body()
                video_path = body.get("video_path", "")
                if not video_path or not os.path.exists(video_path):
                    self._send_json({"error": f"视频文件不存在: {video_path}"}, 400)
                    return
                job_id = _new_job(video_path, body.get("params") or {})
                threading.Thread(target=_run_job,
                                 args=(job_id, video_path, body.get("params") or {}),
                                 daemon=True).start()
                self._send_json({"job_id": job_id})
            elif self.path.startswith("/job/") and self.path.endswith("/cancel"):
                job_id = self.path.split("/")[2]
                job = _JOBS.get(job_id)
                if job is None:
                    self._send_json({"error": "job not found"}, 404)
                else:
                    job["cancel"] = True
                    self._send_json({"ok": True})
            elif self.path == "/rebuild":
                body = self._read_json_body()
                job = _JOBS.get(body.get("job_id", ""))
                if job is None or job.get("context") is None:
                    self._send_json({"error": "job 不存在或上下文不可用"}, 404)
                    return
                import pipeline
                plan = pipeline.rebuild_plan(job["context"], body.get("overrides") or {})
                job["result"] = plan
                self._send_json(plan)
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as e:
            self._send_json({"error": f"{e}", "trace": traceback.format_exc()}, 500)


def main():
    ap = argparse.ArgumentParser(description="NewCut 本地分析服务")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--idle-exit", type=float, default=0.0,
                    help="超过该秒数无 /heartbeat 则自动退出（0=禁用）")
    args = ap.parse_args()

    _HEARTBEAT["idle_exit"] = float(args.idle_exit)
    if _HEARTBEAT["idle_exit"] > 0:
        threading.Thread(target=_watchdog, daemon=True).start()

    _setup_logging()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[{SERVICE_NAME}] v{SERVICE_VERSION} listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
