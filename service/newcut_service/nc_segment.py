# nc_segment.py —— NewCut 自研分段与暂停处理
#
# 规格来自对原工具行为的逆向分析（功能等价，实现为原创）：
#
# 1. 游程分段：连续同状态的帧构成一段。
#
# 2. 暂停段细粒度处理（对应原工具"亮绿色有效操作/红色无效操作"）：
#    - 段内逐帧差分 diff[i] = mean|gray[i]-gray[i-1]|
#    - diff > motion_thresh 的帧视为"有操作"，并连带点亮前一帧（操作起手）
#    - 静止游程若长于 still_frames（默认 0.1s 对应帧数）则删除，
#      且在段首/段尾/中部区别处理，各保留 still_frames 缓冲：
#        段首静止: 保留末尾缓冲帧；段尾静止: 保留开头缓冲帧；
#        中部静止: 前后半分摊缓冲帧
#    - 整段无任何操作时删中段留两端缓冲
#
# 3. 边界差分规则（原工具的核心判据，保留语义）：
#    暂停段前一帧与后一帧的差分 < boundary_thresh，说明暂停前后画面
#    几乎一致（暂停只是跳过了转场/加载），无论段内是否有操作，整段全删。
#
# 4. 倍速段：1x 段标记为建议 2 倍速（PR 模式不做抽帧删除，由 PR 原生
#    速度实现等价效果）；2x 段保持原样。

import cv2
import numpy as np

from nc_states import STATE_PAUSED, STATE_SPEED_1X


def find_runs(states: np.ndarray):
    """连续同状态游程 -> [(state, start, end)]，闭区间。"""
    runs = []
    total = len(states)
    i = 0
    while i < total:
        s = states[i]
        j = i
        while j < total and states[j] == s:
            j += 1
        runs.append((int(s), i, j - 1))
        i = j
    return runs


def _runs_of(mask: np.ndarray):
    """布尔游程 -> [(value, start, end)]，段内闭区间。"""
    out = []
    n = len(mask)
    i = 0
    while i < n:
        v = bool(mask[i])
        j = i
        while j < n and bool(mask[j]) == v:
            j += 1
        out.append((v, i, j - 1))
        i = j
    return out


def pause_delete_mask(s: int, e: int, diffs: np.ndarray, still_frames: int,
                      motion_thresh: float):
    """暂停段 [s,e]（闭区间）的段内删除掩码。返回 (mask(绝对索引), mode)。"""
    seg_len = e - s + 1
    if seg_len <= 0:
        return np.zeros(0, dtype=bool), "auto"
    still_frames = max(2, int(still_frames))

    # 有操作的帧：帧差超阈值，并连带前一帧（操作起手帧）
    active = np.zeros(seg_len, dtype=bool)
    for k in range(1, seg_len):
        if diffs[s + k] > motion_thresh:
            active[k] = True
            active[k - 1] = True

    delete = np.zeros(seg_len, dtype=bool)
    has_active = active.any()
    if not has_active:
        # 整段静止：删中段，两端各留缓冲
        if seg_len > 2 * still_frames:
            delete[still_frames:seg_len - still_frames] = True
        return delete, "auto"

    for v, a, b in _runs_of(~active):  # 静止游程
        if v is False:
            continue
        run_len = b - a + 1
        if run_len <= still_frames:
            continue
        if a == 0:            # 段首静止：保留游程末尾的缓冲
            delete[a:b + 1 - still_frames] = True
        elif b == seg_len - 1:  # 段尾静止：保留游程开头的缓冲
            delete[a + still_frames:b + 1] = True
        else:                  # 中部静止：两端缓冲对半
            half = still_frames // 2
            other = still_frames - half
            delete[a + half:b + 1 - other] = True
    return delete, "auto"


def boundary_diff_targets(pauses: list, total: int):
    """需要二次扫描取帧的索引集合（每个暂停段的前一帧与后一帧）。"""
    idx = set()
    for p in pauses:
        idx.add(max(0, p["start"] - 1))
        idx.add(min(total - 1, p["end"] + 1))
    return sorted(idx)


def scan_frames_grays(video_path: str, proc_size: tuple, target_indices: list,
                      progress_cb=None):
    """顺序 grab 扫描，取指定索引帧的处理分辨率灰度图。"""
    want = sorted(set(target_indices))
    out = {}
    if not want:
        return out
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"无法打开视频: {video_path}")
    cur = 0
    for t in want:
        while cur < t:
            cap.grab()
            cur += 1
        ok, frame = cap.read()
        cur += 1
        if ok:
            out[t] = cv2.cvtColor(
                cv2.resize(frame, proc_size, interpolation=cv2.INTER_AREA),
                cv2.COLOR_BGR2GRAY)
        if progress_cb:
            progress_cb(t / max(1, want[-1]))
    cap.release()
    return out


def boundary_diff(grays: dict, s: int, e: int, total: int) -> float:
    b = max(0, s - 1)
    a = min(total - 1, e + 1)
    if b not in grays or a not in grays:
        return 0.0
    return float(cv2.mean(cv2.absdiff(grays[b], grays[a]))[0])
