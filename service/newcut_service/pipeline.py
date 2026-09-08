# pipeline.py —— NewCut 分析流水线 v3
#
# v3 新增：
#   - 解码层三级回退（decode.py）：ffmpeg NVDEC 硬解 / ffmpeg 软解 / OpenCV，
#     解码+缩放下沉 ffmpeg（实测 ~1100 fps），Python 只收小尺寸灰度帧
#   - 快速模式：每 N 帧采样分类 + 前值填充（牺牲边界精度换取速度）
#   - 转场删除：黑屏段 + 冻结段（NORMAL 态长静止）自动标记删除
#   - 碎片合并：相邻删除段之间小于 merge_gap 的保留缝隙并入删除
#   - 上下文缓存：states/diffs/means/暂停掩码留在服务端，/rebuild 免重解码
#   - 取消回调
#
# 与原工具功能对应关系见 docs/逆向实现规格.md。

import os
import time

import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor

import decode
import nc_match
import nc_segment
from nc_states import STATE_PAUSED, STATE_SPEED_1X

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE_DIR = os.path.join(_PKG_DIR, "template_pack")

DEFAULT_PARAMS = {
    "thresholds": {"paused": None, "running": None, "speed1x": None, "speed2x": None},
    "still_time_thresh": 0.1,
    "motion_thresh": 2.0,
    "boundary_thresh": 5.0,
    "speedup_1x": True,
    "template_dir": None,
    "decode_backend": "auto",       # auto | ffmpeg | opencv
    "fast_mode": False,             # 每 N 帧采样分类（OpenCV 慢路径提速用）
    "merge_gap_sec": 0.3,           # 相邻删除段间隙小于该值则并入删除
    "detect_transitions": True,     # 黑屏/冻结段自动删除
    "excluded_pauses": [],          # 人工排除的暂停段 id
}


def probe_video(video_path: str) -> dict:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"无法打开视频文件: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if fps <= 0 or frames <= 0:
        raise ValueError(f"视频元数据异常 (fps={fps}, frames={frames})")
    return {"fps": fps, "frame_count": frames, "width": w, "height": h,
            "duration_sec": frames / fps}


def _load_pack(params: dict) -> nc_match.TemplatePack:
    d = params.get("template_dir") or DEFAULT_TEMPLATE_DIR
    if not os.path.isfile(os.path.join(d, "manifest.json")):
        raise ValueError(
            f"模板包不存在: {d}\n"
            "请先用校准工具生成： uv run python tools/calibrate.py --help")
    return nc_match.TemplatePack(d)


class Cancelled(Exception):
    pass


def _sample_frames(video_path: str, info: dict, pack, k: int = 10) -> list:
    """取 k 个均匀分布的采样帧（proc_size 灰度），供几何自适应。"""
    total = max(1, info["frame_count"])
    idxs = sorted({int(total * f) for f in
                   (0.02, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.98)})[:k + 2]
    out = []
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return out
    try:
        for i in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, f = cap.read()
            if ok:
                out.append(nc_match.preprocess(f, pack.proc_size))
            if len(out) >= k:
                break
    finally:
        cap.release()
    return out


def _classify_all(video_path, pack, p, info, n_threads, progress_cb, cancel_cb):
    """解码 + 逐帧分类。返回 (states, diffs, means, combat, backend_name)。"""
    fps = info["fps"]
    total_est = max(1, info["frame_count"])
    backend = p.get("decode_backend", "auto")
    step = max(2, int(round(fps / 12))) if p.get("fast_mode") else 1

    def report(frac, detail):
        if progress_cb:
            progress_cb("decode", max(0.0, min(1.0, frac)), detail)

    def classify_all_from(gray_iter, pre_sized, label):
        if pre_sized:
            def work(g):
                g2 = decode.fit_to_pack(g, pack.proc_size)
                (st, cb), mean, g2 = nc_match.classify_full(g2, pack), float(np.mean(g2)), g2
                return st, cb, mean, g2
        else:
            def work(f):
                g = nc_match.preprocess(f, pack.proc_size)
                (st, cb), mean, g = nc_match.classify_full(g, pack), float(np.mean(g)), g
                return st, cb, mean, g

        states_l, means_l, diffs_l, combat_l = [], [], [], []
        prev_gray = [None]
        count = [0]
        t0 = time.time()

        def consume(batch):
            jobs = [(i, f) for i, f in enumerate(batch) if f is not None]
            results = {}
            if jobs:
                results = dict(zip([i for i, _ in jobs],
                                   pool.map(work, [f for _, f in jobs])))
            for i, f in enumerate(batch):
                count[0] += 1
                if i in results:
                    st, cb, mean, g = results[i]
                    d = 0.0
                    if prev_gray[0] is not None and prev_gray[0].shape == g.shape:
                        d = float(cv2.mean(cv2.absdiff(prev_gray[0], g))[0])
                    prev_gray[0] = g
                    states_l.append(st); means_l.append(mean); diffs_l.append(d)
                    combat_l.append(cb)
                else:
                    # 快速模式跳过的帧：继承前一个采样帧的状态（前值填充）
                    states_l.append(states_l[-1] if states_l else 0)
                    means_l.append(means_l[-1] if means_l else 0.0)
                    diffs_l.append(0.0)
                    combat_l.append(combat_l[-1] if combat_l else False)
            report(count[0] / total_est, f"解码与帧分类 ({label})")

        with ThreadPoolExecutor(max_workers=max(1, n_threads)) as pool:
            batch = []
            for fr in gray_iter:
                if cancel_cb and cancel_cb():
                    raise Cancelled()
                take = (step == 1) or (count[0] % step == 0)
                batch.append(fr if take else None)
                if len(batch) >= 256:
                    consume(batch); batch = []
            if batch:
                consume(batch)

        print(f"[nc-engine] 后端={label} 分类完成: {count[0]} 帧, "
              f"{count[0] / max(time.time() - t0, 0.001):.0f} fps", flush=True)
        return (np.array(states_l, dtype=np.int8),
                np.array(diffs_l, dtype=np.float32),
                np.array(means_l, dtype=np.float32),
                np.array(combat_l, dtype=bool))

    # ---- 路径选择：ffmpeg(实测选型 NVDEC/QSV/DXVA/软解) -> OpenCV ----
    if backend in ("auto", "ffmpeg", "sw", "cuda", "qsv", "d3d11va", "dxva2",
                   "videotoolbox", "vaapi", "vdpau"):
        try:
            requested = "auto" if backend == "auto" else ("sw" if backend == "ffmpeg" else backend)
            frames, backend_name = decode.iter_gray_auto(video_path, pack.proc_size,
                                                         requested)
            if frames is None:
                raise RuntimeError("ffmpeg 不可用")
            st, df, mn, cb = classify_all_from(frames, True, backend_name)
            warn = None
            if backend not in ("auto", "ffmpeg", "sw") and backend_name != f"ffmpeg-{backend}":
                warn = (f"GPU 加速未生效：请求 {backend}，实际使用 {backend_name}"
                        f"（探测未达标或硬件不可用），已自动回退软解")
            return st, df, mn, cb, backend_name, warn
        except Cancelled:
            raise
        except Exception as e:
            if backend not in ("auto",):
                raise ValueError(f"ffmpeg 解码失败: {e}")
            print(f"[nc-engine] ffmpeg 路径失败({e})，回退 OpenCV", flush=True)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"无法打开视频文件: {video_path}")

    def bgr_iter():
        while True:
            ok, f = cap.read()
            if not ok:
                break
            yield f

    try:
        st, df, mn, cb = classify_all_from(bgr_iter(), False, "opencv")
    finally:
        cap.release()
    return st, df, mn, cb, "opencv", None


def _runs_true(mask) -> list:
    """布尔数组中 True 游程 -> [(start, end)] 闭区间。"""
    mask = np.asarray(mask, dtype=bool)
    if mask.size == 0:
        return []
    edges = np.diff(np.concatenate(([0], mask.view(np.int8), [0])))
    starts = np.where(edges == 1)[0]
    ends = np.where(edges == -1)[0] - 1
    return list(zip(starts.tolist(), ends.tolist()))


def _complement(keep, total):
    out, prev = [], 0
    for s, e in keep:
        if s > prev:
            out.append([prev, s])
        prev = e
    if prev < total:
        out.append([prev, total])
    return out


def _build_plan(video_path, info, pack, states, diffs, means, combat, p,
                progress_cb, fast_mode):
    fps, total = info["fps"], len(states)

    def report(phase, frac, detail=""):
        if progress_cb:
            progress_cb(phase, max(0.0, min(1.0, frac)), detail)

    # ---- 游程分段 ----
    report("segments", 0.9, "分段与暂停处理")
    pauses, speeds = [], []
    for st, s, e in nc_segment.find_runs(states):
        if st == STATE_PAUSED:
            pauses.append({"id": len(pauses), "start": s, "end": e})
        elif st == STATE_SPEED_1X:
            speeds.append({"type": "1x", "start": s, "end": e, "factor": 2.0})

    # ---- 暂停段内运动分析 ----
    still_frames = max(2, fps * float(p["still_time_thresh"]))
    for seg in pauses:
        mask, mode = nc_segment.pause_delete_mask(
            seg["start"], seg["end"], diffs, still_frames, float(p["motion_thresh"]))
        seg["local_del"] = mask
        seg["mode"] = mode

    # ---- 边界差分 ----
    if pauses:
        report("boundary", 0.92, "边界差分扫描")
        targets = nc_segment.boundary_diff_targets(pauses, total)
        grays = nc_segment.scan_frames_grays(video_path, pack.proc_size, targets)
        for seg in pauses:
            bd = nc_segment.boundary_diff(grays, seg["start"], seg["end"], total)
            seg["boundary_diff"] = bd
            if bd < float(p["boundary_thresh"]):
                seg["mode"] = "all"

    # ---- 删除掩码（含人工排除） ----
    warnings = []
    excluded = set(p.get("excluded_pauses") or [])
    to_del = np.zeros(total, dtype=bool)
    for seg in pauses:
        if seg["id"] in excluded:
            continue
        s, e = seg["start"], seg["end"]
        if seg["mode"] == "all":
            to_del[s:e + 1] = True
        else:
            to_del[s:e + 1] |= seg["local_del"].astype(bool)

    # ---- 转场删除：仅黑屏（参考项目不删除 NORMAL 冻结帧——安静等待是
    #      正常玩法，帧差低不代表该删；此前误删大量正常画面，已移除） ----
    transitions = []
    if p.get("detect_transitions", True):
        report("transitions", 0.95, "转场检测")
        dark = means < 22
        min_black = max(2, int(fps * 0.4))
        for s, e in _runs_true(dark):
            if e - s + 1 >= min_black:
                to_del[s:e + 1] = True
                transitions.append({"type": "black", "start": s, "end": e})

    # ---- 非作战画面删除：combat 标签（"剩余可放置角色"）缺失的游程 ----
    # 双保险（此前 combat 模板在布局不同的录屏里全部落空，整段视频被误判
    # 非作战而几乎全删，26 分钟只剩 2 秒）：
    #   1. 全片 combat 周占比 >= min_combat_ratio 才启用（作战画面应占多数）
    #   2. 删除量超过总量 60% 自动熔断，保留原样并报警
    noncombat = []
    if pack.has_combat and combat is not None and p.get("remove_noncombat", True):
        report("noncombat", 0.97, "非作战画面检测")
        combat_ratio = float(np.mean(combat.astype(bool)))
        if combat_ratio < 0.05:
            warnings.append(
                f"combat 标签命中率仅 {combat_ratio * 100:.1f}%（<5%），非作战画面删除"
                f"已自动停用；模板包与本次录屏布局可能不匹配，请用 --combat 重新校准")
            print(f"[nc-engine] 警告: {warnings[-1]}", flush=True)
        else:
            cand = []
            for s, e in _runs_true(~combat.astype(bool)):
                if e - s + 1 >= max(2, int(fps * 0.3)):
                    cand.append((s, e))
            would_del = sum(e - s + 1 for s, e in cand)
            if total > 0 and would_del > total * 0.6:
                warnings.append(
                    f"非作战删除将移除 {would_del * 100 // max(1, total)}% 的画面，"
                    f"超过 60% 安全线，已自动熔断停用")
                print(f"[nc-engine] 警告: {warnings[-1]}", flush=True)
            else:
                for s, e in cand:
                    to_del[s:e + 1] = True
                    noncombat.append({"start": s, "end": e})

    # ---- 全局 sanity：暂停帧占比过高 / 保留过少 -> 警告 ----
    paused_frac = float(np.mean(states == STATE_PAUSED)) if total else 0.0
    if paused_frac > 0.8:
        warnings.append(
            f"暂停帧占比 {paused_frac * 100:.0f}% 异常偏高，模板匹配可能失效"
            f"（布局/分辨率不匹配？），结果可信度低")
        print(f"[nc-engine] 警告: {warnings[-1]}", flush=True)

    keep_ranges = [[int(a), int(b) + 1] for a, b in _runs_true(~to_del)]

    # ---- 碎片合并：相邻删除段之间的短保留缝隙并入删除 ----
    gap_frames = int(round(fps * float(p.get("merge_gap_sec", 0.3))))
    if gap_frames > 0 and len(keep_ranges) > 1:
        kept = [keep_ranges[0]]
        for kr in keep_ranges[1:]:
            if kr[0] - kept[-1][1] < gap_frames:
                continue
            kept.append(kr)
        keep_ranges = kept

    delete_ranges = _complement(keep_ranges, total)

    pause_events = []
    for seg in pauses:
        s, e = seg["start"], seg["end"]
        if seg["id"] in excluded:
            sub_keep = [[s, e + 1]]
        elif seg["mode"] == "all":
            sub_keep = []
        else:
            alive = ~(seg["local_del"].astype(bool))
            sub_keep = [[s + int(a), s + int(b) + 1] for a, b in _runs_true(alive)]
        pause_events.append({
            "id": seg["id"], "start": s, "end": e,
            "mode": seg["mode"],
            "excluded": seg["id"] in excluded,
            "boundary_diff": round(float(seg.get("boundary_diff", 0.0)), 2),
            "sub_keep_ranges": sub_keep,
        })

    kept = sum(e - s for s, e in keep_ranges)
    if total > 0 and kept < total * 0.05 and total / fps > 60:
        warnings.append(
            f"保留时长仅 {kept / fps:.1f}s（原片 {total / fps:.0f}s，<5%），"
            f"删除规则可能误判，请核对面板勾选或附日志反馈")
        print(f"[nc-engine] 警告: {warnings[-1]}", flush=True)
    report("done", 1.0, "完成")
    return {
        "engine": "newcut-nc/1.1",
        "video_path": video_path,
        "fps": fps, "frame_count": total,
        "width": info["width"], "height": info["height"],
        "duration_sec": total / fps,
        "edited_duration_sec": kept / fps,
        "pauses": pause_events,
        "speeds": [sp for sp in speeds if p["speedup_1x"]],
        "transitions": transitions,
        "noncombat_ranges": noncombat,
        "warnings": warnings,
        "keep_ranges": keep_ranges,
        "delete_ranges": delete_ranges,
        "total_kept_frames": kept,
        "deleted_frame_count": total - kept,
    }


def run_analysis_full(video_path: str, params: dict, progress_cb=None,
                      cancel_cb=None):
    """完整分析。返回 (方案JSON, 可重建上下文)。"""
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})
    thr_in = dict(p["thresholds"] or {})
    for old, new in (("pause", "paused"), ("speed_1x", "speed1x"), ("speed_2x", "speed2x")):
        if old in thr_in and new not in thr_in:
            thr_in[new] = thr_in[old]
    p["thresholds"] = thr_in

    def report(phase, frac, detail=""):
        if progress_cb:
            progress_cb(phase, max(0.0, min(1.0, frac)), detail)

    info = probe_video(video_path)
    report("load", 0.0, "加载模板")
    pack = _load_pack(p)
    for patch in pack.patches:
        override = p["thresholds"].get(patch["state"])
        if override:
            patch["thresh"] = float(override)

    n_threads = int(params.get("n_threads") or 0) or (os.cpu_count() or 4)
    fast_mode = bool(p.get("fast_mode"))

    # ---- 几何自适应：采样帧锁定 UI 缩放比与控制条位置 ----
    report("load", 0.0, "几何自适应")
    geometry = None
    try:
        samples = _sample_frames(video_path, info, pack)
        if len(samples) >= 3:
            geometry = nc_match.calibrate_geometry(samples, pack)
    except Exception as e:
        print(f"[nc-engine] 几何自适应失败（按原始模板继续）: {e}", flush=True)

    report("decode", 0.0, "解码与帧分类")
    states, diffs, means, combat, backend, backend_warn = _classify_all(
        video_path, pack, p, info, n_threads, progress_cb, cancel_cb)
    total = len(states)
    if len(diffs) != total:
        diffs = np.zeros(total, dtype=np.float32)
    if combat is None or len(combat) != total:
        combat = np.zeros(total, dtype=bool)

    result = _build_plan(video_path, info, pack, states, diffs, means, combat, p,
                         progress_cb, fast_mode)
    result["backend"] = backend
    result["backend_warning"] = backend_warn
    result["geometry"] = geometry
    result["decode_bench"] = {} if backend == "opencv" else dict(decode.LAST_BENCH)
    result["fast_mode"] = fast_mode
    context = {
        "video_path": video_path, "info": info, "proc_size": list(pack.proc_size),
        "states": states, "diffs": diffs, "means": means, "combat": combat,
        "params": p, "fast_mode": fast_mode,
    }
    return result, context


def rebuild_plan(context: dict, overrides: dict) -> dict:
    """基于缓存上下文重算方案（不重新解码）。"""
    p = dict(context["params"])
    for k in ("excluded_pauses", "merge_gap_sec", "detect_transitions",
              "remove_noncombat", "still_time_thresh", "motion_thresh",
              "boundary_thresh"):
        if k in (overrides or {}):
            p[k] = overrides[k]
    pack = _load_pack(p)
    combat = context.get("combat")
    return _build_plan(context["video_path"], context["info"], pack,
                       context["states"], context["diffs"], context["means"],
                       combat, p, None, context.get("fast_mode", False))


def run_analysis(video_path: str, params: dict, progress_cb=None) -> dict:
    """兼容旧签名（冒烟测试等）。"""
    result, _ = run_analysis_full(video_path, params, progress_cb)
    return result
