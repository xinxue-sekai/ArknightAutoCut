# decode.py —— 解码层 v3：跨显卡自适应
#
# 设计：不猜测显卡型号，而是"实测选型"。启动分析时用候选硬件加速
# 后端各试解一小段视频，测得最快的后端胜出并缓存（同一会话内复用）。
#
# 候选与适配：
#   cuda          NVIDIA (NVDEC)
#   qsv           Intel 核显 (Quick Sync)
#   d3d11va/dxva2 Windows 通用 GPU 解码（NVIDIA/AMD/Intel 均可能支持）
#   videotoolbox  macOS
#   vaapi/vdpau   Linux
#   sw            纯 CPU 软解（始终参与竞争的保底基线）
#   opencv        无 ffmpeg 时的最后回退
#
# 实测参考（RTX 5060, 1920x1200@60fps H.264）：各后端均在 ~1100 fps，
# 此时分类才是瓶颈；4K 或低配 CPU 上硬解优势才会显现——所以用实测选
# 而非硬编码偏好。

import os
import shutil
import subprocess
import threading

import cv2
import numpy as np

# 各平台候选（按顺序仅用于展示；实际顺序由实测决定）
CANDIDATE_HWACCELS = ["cuda", "qsv", "d3d11va", "dxva2", "videotoolbox",
                      "vaapi", "vdpau"]
PROBE_FRAMES = 96          # 选型探测帧数
PROBE_MIN_FPS = 30         # 低于此值视为该后端不可用
HW_PREFER_MARGIN = 1.10    # 硬解需比软解快 10% 以上才优先（软解最稳）

_lock = threading.Lock()
_selected = None  # {"hwaccel": str|None, "label": str} 全会话缓存（仅 auto 模式写入）
LAST_BENCH = {}   # 最近一次实测的各后端 fps（供面板展示）
LAST_LABEL = ""

# pythonw（无控制台）下拉起 ffmpeg 等控制台程序会弹窗，必须显式隐藏
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

LOG_SINK = None  # 由 server._setup_logging 注入日志文件对象；ffmpeg 报错写到这里


def _set_last(label, bench):
    global LAST_BENCH, LAST_LABEL
    LAST_BENCH, LAST_LABEL = bench, label


def find_ffmpeg():
    exe = os.environ.get("NEWCUT_FFMPEG")
    if exe and os.path.isfile(exe):
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which("ffmpeg")


def available_hwaccels(ffmpeg):
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-hwaccels"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        listed = [ln.strip() for ln in r.stdout.splitlines()[1:] if ln.strip()]
        return [h for h in CANDIDATE_HWACCELS if h in listed]
    except Exception:
        return []


def _bench_once(ffmpeg, video_path, tw, th, hwaccel, frames):
    """试解 frames 帧，返回 fps；失败返回 -1。"""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    cmd += ["-i", video_path, "-frames:v", str(frames),
            "-vf", f"scale={tw}:{th},format=gray",
            "-f", "rawvideo", "pipe:1"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=2 ** 24,
                                creationflags=CREATE_NO_WINDOW)
    except Exception:
        return -1
    bpf = tw * th
    n = 0
    t0 = __import__("time").time()
    try:
        while n < frames:
            buf = proc.stdout.read(bpf)
            if len(buf) < bpf:
                break
            n += 1
    finally:
        try:
            proc.kill()
        except Exception:
            pass
    dt = __import__("time").time() - t0
    if n < frames * 0.5 or dt <= 0:  # 帧数不足说明后端中途失败
        return -1
    return n / dt


def select_backend(video_path: str, tw: int, th: int, requested: str = "auto"):
    """选型。requested: auto|sw|cuda|qsv|d3d11va|dxva2|videotoolbox|vaapi|vdpau

    返回 (hwaccel: str|None, label: str, bench: dict)。
    auto 模式：所有可用候选实测竞争，硬解需快 10%+ 才胜出，否则软解保底。
    显式指定某硬件后端时直接采用（失败由上层回退链兜底）。
    """
    global _selected
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None, "opencv", {}

    with _lock:
        if requested == "auto" and _selected:
            return _selected["hwaccel"], _selected["label"], _selected["bench"]

    bench = {}
    if requested not in (None, "", "auto", "sw"):
        bench[requested] = _bench_once(ffmpeg, video_path, tw, th, requested, PROBE_FRAMES)
        hwaccel = requested if bench[requested] >= PROBE_MIN_FPS else None
        label = f"ffmpeg-{requested}" if hwaccel else "ffmpeg-sw"
        _set_last(label, bench)  # 强制模式不写入会话缓存，避免污染后续 auto 选型
        return hwaccel, label, bench

    if requested == "sw":
        _set_last("ffmpeg-sw", {})
        return None, "ffmpeg-sw", {}

    # ---- auto：实测竞争 ----
    cands = [None] + available_hwaccels(ffmpeg)
    for hw in cands:
        bench[hw or "sw"] = _bench_once(ffmpeg, video_path, tw, th, hw, PROBE_FRAMES)

    sw_fps = bench.get("sw", -1)
    best_hw, best_fps = None, -1
    for hw in cands:
        if hw is None:
            continue
        f = bench.get(hw, -1)
        if f > best_fps:
            best_hw, best_fps = hw, f

    if best_hw and best_fps >= sw_fps * HW_PREFER_MARGIN and best_fps >= PROBE_MIN_FPS:
        result = {"hwaccel": best_hw, "label": f"ffmpeg-{best_hw}", "bench": bench}
    else:
        result = {"hwaccel": None, "label": "ffmpeg-sw", "bench": bench}
    with _lock:
        _selected = result
    _set_last(result["label"], bench)
    print(f"[nc-engine] 解码选型: {result['label']} "
          f"({', '.join(f'{k}={v:.0f}fps' for k, v in bench.items() if v > 0)})", flush=True)
    return result["hwaccel"], result["label"], bench


def target_dims(video_path: str, proc_w: int):
    """按视频宽高比等比计算 ffmpeg 输出尺寸（宽固定 proc_w，高取偶）。"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"无法打开视频: {video_path}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    th = max(2, int(round(proc_w * h / max(1, w)) // 2 * 2))
    return proc_w, th


def _ffmpeg_proc(ffmpeg, video_path, tw, th, hwaccel):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    cmd += ["-i", video_path,
            "-vf", f"scale={tw}:{th},format=gray",
            "-f", "rawvideo", "pipe:1"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=2 ** 24, creationflags=CREATE_NO_WINDOW)


def iter_gray_ffmpeg(video_path: str, tw: int, th: int, hwaccel: str = None):
    """生成器：逐帧产出 (th, tw) 灰度帧。ffmpeg 路径专用。

    ffmpeg 异常退出（解码中断→分析提前结束）时，把 stderr 尾巴写进
    日志文件——之前 DEVNULL 丢弃导致这类问题完全无法排查。
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg 不可用")
    proc = _ffmpeg_proc(ffmpeg, video_path, tw, th, hwaccel)
    bpf = tw * th
    err_tail = []  # 保留最后 30 行 stderr，仅在异常退出时输出
    if proc.stderr is not None:
        def _drain():
            try:
                for line in proc.stderr:
                    err_tail.append(line)
                    if len(err_tail) > 30:
                        err_tail.pop(0)
            except Exception:
                pass
        threading.Thread(target=_drain, daemon=True).start()
    try:
        while True:
            buf = proc.stdout.read(bpf)
            if len(buf) < bpf:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(th, tw)
        rc = None
        try:
            rc = proc.wait(timeout=10)
        except Exception:
            pass
        if rc not in (0, None) and LOG_SINK is not None:
            print(f"[nc-engine] ffmpeg 解码提前中断 rc={rc} "
                  f"(hwaccel={hwaccel}): {''.join(err_tail)[-2000:]}",
                  flush=True)
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def iter_gray_auto(video_path: str, proc_size, requested: str = "auto"):
    """自动选型的解码生成器。返回 (frames_iter, backend_name)。

    frames 元素为等比缩放的灰度帧（尺寸可能与模板包 proc_size 不同，
    由 classify 前的 fit_to_pack 做黑边填充）。
    """
    tw, th0 = proc_size
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None, "opencv"
    try:
        tw, th = target_dims(video_path, tw)
    except Exception:
        tw, th = proc_size

    hwaccel, label, _ = select_backend(video_path, tw, th, requested)
    try:
        it = iter_gray_ffmpeg(video_path, tw, th, hwaccel)
        first = next(it)  # 拉一帧验证后端可用（不可用则走回退）
    except Exception:
        if hwaccel:  # 选型时可用、正式解码失败（罕见）：降级软解再试一次
            try:
                it = iter_gray_ffmpeg(video_path, tw, th, None)
                first = next(it)
                label = "ffmpeg-sw"
            except Exception:
                return None, "opencv"
        else:
            return None, "opencv"

    def gen():
        yield first
        yield from it
    return gen(), label


def fit_to_pack(gray: np.ndarray, proc_size: tuple) -> np.ndarray:
    """ffmpeg 输出的等比帧与模板包 proc_size 不一致时做黑边填充。"""
    tw, th = proc_size
    if gray.shape == (th, tw):
        return gray
    h, w = gray.shape[:2]
    scale = min(tw / w, th / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = cv2.resize(gray, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((th, tw), dtype=np.uint8)
    x0, y0 = (tw - nw) // 2, (th - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = resized
    return canvas
