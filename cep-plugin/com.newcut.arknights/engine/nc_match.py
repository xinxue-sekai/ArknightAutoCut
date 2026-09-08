# nc_match.py —— NewCut 自研帧状态匹配器 v3
#
# v1 的教训（素材 1920x1200 / 带窗口标题栏 / 16:10 导致 ROI 全部落空）：
#   匹配不再假设 UI 位置。参考原项目 arknight-auto-editing 的两个关键设计：
#     1) 掩码 NCC：模板按亮度阈值二值化，只匹配亮色图标像素（抗底板/背景干扰）
#     2) 搜索窗口放宽（原项目为 2 倍 ROI；v2 直接在限定区域内全帧搜索）
#   并改为等比缩放（黑边填充），杜绝 16:10 / 16:9 / 不同窗口布局造成的形变。
#
# v3 新增（录屏为"桌面+模拟器内嵌窗口"等布局时 v2 模板尺寸/位置全部落空）：
#   - 几何自适应 calibrate_geometry()：分析开始前用少量采样帧做
#     多尺度模板搜索，锁定本次视频的 UI 缩放比与控制条位置，
#     后续每帧用缩放后的模板、只在锁定位置附近的小窗口匹配
#   - combat 模板组：匹配作战画面特有的"剩余可放置角色"标签，
#     用于区分作战/非作战画面（菜单/加载/编队/结算整段删除）
#
# 判定逻辑：
#   playpause 组（右键）最佳匹配 paused(▶) 且过阈值      -> PAUSED
#   playpause 组最佳匹配 running(❚❚) 且过阈值            -> 看速度组：
#       speed 组匹配 speed1x -> SPEED_1X；speed2x -> SPEED_2X；否则 NORMAL
#   无任何过阈值匹配                                      -> NORMAL
#   combat 组（"剩余可放置角色"标签）过阈值              -> 作战画面

import json
import os

import cv2
import numpy as np

from nc_states import STATE_NORMAL, STATE_PAUSED, STATE_SPEED_1X, STATE_SPEED_2X

DEFAULT_ROIS = {
    "speed": [0.812, 0.015, 0.900, 0.125],
    "playpause": [0.912, 0.015, 0.995, 0.125],
}
# 全帧搜索限制在画面右上区域（控制条只可能在这里出现，且避免场景误报）
DEFAULT_SEARCH_REGION = [0.35, 0.0, 1.0, 0.55]
# combat 组默认搜索区域：作战 UI 的"剩余可放置角色"标签在游戏区下缘，
# 内嵌窗口布局下位置不定，先放宽为整个画面（几何锁定后再收窄）
DEFAULT_COMBAT_REGION = [0.0, 0.0, 1.0, 1.0]

# 几何自适应：模板相对校准基准的候选缩放比（覆盖窗口内嵌/小窗录屏/高分屏）
GEOMETRY_SCALES = [0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 1.0, 1.15, 1.3, 1.5]
GEOMETRY_MIN_SCORE = 0.55   # 全部尺度都低于该分说明找不到游戏 UI
GEOMETRY_MARGIN = 2.5       # 锁定后搜索窗口 = 匹配位置 ± 模板尺寸*该系数

MANIFEST_NAME = "manifest.json"


class TemplatePack:
    """模板包：若干补丁（含亮像素掩码）+ 所属按钮组 + 置信度阈值。"""

    def __init__(self, pack_dir: str):
        self.dir = pack_dir
        with open(os.path.join(pack_dir, MANIFEST_NAME), "r", encoding="utf-8") as f:
            self.manifest = json.load(f)
        self.proc_size = tuple(self.manifest["proc_size"])  # (w, h) 等比缩放目标
        self.rois = {k: tuple(v) for k, v in self.manifest.get("rois", DEFAULT_ROIS).items()}
        self.search_region = tuple(self.manifest.get("search_region", DEFAULT_SEARCH_REGION))
        self.combat_region = tuple(self.manifest.get("combat_region", DEFAULT_COMBAT_REGION))
        self.patches = []
        for p in self.manifest["patches"]:
            img = cv2.imread(os.path.join(pack_dir, p["file"]), cv2.IMREAD_GRAYSCALE)
            if img is None:
                raise ValueError(f"模板包补丁缺失: {p['file']}")
            mask_thresh = float(p.get("mask_thresh", 130))
            mask = (img > mask_thresh).astype(np.uint8) * 255
            self.patches.append({
                "name": p["name"], "state": p["state"], "roi": p["roi"],
                "thresh": float(p.get("thresh", 0.75)),
                "gray": img, "mask": mask,
            })
        if not self.patches:
            raise ValueError("模板包为空")
        self._region_cache = {}
        # 几何自适应结果（calibrate_geometry 写入）
        self.geometry = None  # {"scale": float, "score": float, "warn": str|None}

    @property
    def has_combat(self) -> bool:
        return any(p["roi"] == "combat" for p in self.patches)

    def _window(self, region, shape):
        key = (region, shape)
        if key not in self._region_cache:
            h, w = shape[:2]
            x0 = int(region[0] * w); y0 = int(region[1] * h)
            x1 = max(x0 + 1, int(region[2] * w))
            y1 = max(y0 + 1, int(region[3] * h))
            self._region_cache[key] = (x0, y0, x1, y1)
        return self._region_cache[key]

    def search_window(self, shape):
        """归一化搜索区域 -> 像素裁剪框（按帧尺寸缓存）。"""
        return self._window(self.search_region, shape)

    def combat_window(self, shape):
        return self._window(self.combat_region, shape)

    def _scaled(self, p, scale):
        """返回模板在指定缩放比下的 (gray, mask)，按 (name, scale) 缓存。"""
        key = (p["name"], round(scale, 3))
        cache = self.__dict__.setdefault("_scale_cache", {})
        if key not in cache:
            h, w = p["gray"].shape[:2]
            nw, nh = max(2, int(round(w * scale))), max(2, int(round(h * scale)))
            g = cv2.resize(p["gray"], (nw, nh), interpolation=cv2.INTER_AREA)
            m = cv2.resize(p["mask"], (nw, nh), interpolation=cv2.INTER_NEAREST)
            cache[key] = (g, m)
        return cache[key]

    def _set_window_around(self, region_norm, positions, patch_shape, shape_hw):
        """把某组的搜索窗口收窄到若干采样匹配位置的中位数附近。"""
        if not positions:
            return
        h, w = shape_hw
        pw, ph = patch_shape[1], patch_shape[0]
        xs = sorted(p[0] for p in positions)
        ys = sorted(p[1] for p in positions)
        cx, cy = xs[len(xs) // 2] + pw / 2, ys[len(ys) // 2] + ph / 2
        mx, my = pw * GEOMETRY_MARGIN, ph * GEOMETRY_MARGIN
        x0, y0 = max(0, (cx - mx) / w), max(0, (cy - my) / h)
        x1, y1 = min(1.0, (cx + mx) / w), min(1.0, (cy + my) / h)
        return (x0, y0, x1, y1)


def preprocess(frame_bgr: np.ndarray, proc_size: tuple) -> np.ndarray:
    """BGR 原帧 -> 等比缩放 + 居中黑边填充到 proc_size 的灰度帧。

    等比缩放保证模板形状永不变形；宽高比与模板包一致的素材恰好铺满。
    """
    tw, th = proc_size
    h, w = frame_bgr.shape[:2]
    scale = min(tw / w, th / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    if (nw, nh) == (tw, th):
        return gray
    canvas = np.zeros((th, tw), dtype=np.uint8)
    x0, y0 = (tw - nw) // 2, (th - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = gray
    return canvas


def _masked_score(region: np.ndarray, patch_gray: np.ndarray, patch_mask: np.ndarray) -> tuple:
    """掩码 NCC：只对模板亮像素计算归一化互相关。返回 (最大分, 左上位置)。"""
    if region.shape[0] < patch_gray.shape[0] or region.shape[1] < patch_gray.shape[1]:
        return -1.0, (0, 0)
    res = cv2.matchTemplate(region, patch_gray, cv2.TM_CCOEFF_NORMED, mask=patch_mask)
    res = np.where(np.isfinite(res), res, -1.0)
    if res.size == 0:
        return -1.0, (0, 0)
    idx = np.unravel_index(np.argmax(res), res.shape)
    return float(res[idx]), (int(idx[1]), int(idx[0]))


def calibrate_geometry(sample_grays: list, pack: TemplatePack) -> dict:
    """多尺度几何自适应：用少量采样帧锁定 UI 缩放比与控制条位置。

    对每个候选尺度，用 playpause 组模板在采样帧上做全帧搜索，取分数
    中位数最高的尺度；再把各组的搜索窗口收窄到中位匹配位置附近。
    之后所有帧都用缩放后的模板匹配，窗口外的干扰像素被直接排除。
    """
    if not sample_grays:
        return {"scale": 1.0, "score": 0.0, "warn": "无采样帧，跳过几何自适应"}

    shape = sample_grays[0].shape[:2]
    main_patches = [p for p in pack.patches if p["roi"] in ("playpause", "speed")]
    combat_patches = [p for p in pack.patches if p["roi"] == "combat"]

    scale_scores = {}
    for scale in GEOMETRY_SCALES:
        scores = []
        for g in sample_grays:
            s = -1.0
            for p in main_patches:
                pg, pm = pack._scaled(p, scale)
                sc, _ = _masked_score(g, pg, pm)
                s = max(s, sc)
            scores.append(s)
        med = float(np.median(scores))
        scale_scores[round(scale, 2)] = round(med, 3)

    # 极小模板会在任意画面刷出虚高 NCC，与最佳分接近时优先更大尺度
    # （大模板判别力更强、误报更少）
    med = max(scale_scores.values())
    scale = max(s for s, m in scale_scores.items() if m >= med - 0.02)
    warn = None
    if med < GEOMETRY_MIN_SCORE:
        warn = (f"几何自适应置信度低（最佳 {med:.2f} < {GEOMETRY_MIN_SCORE}），"
                f"请确认录屏里有游戏对局控制条，或重新校准模板包")
        scale = 1.0

    # 应用：主组模板按锁定的尺度缩放，搜索窗口收窄到中位匹配位置附近
    positions = []
    for g in sample_grays:
        bs, bloc = -1.0, None
        for p in main_patches:
            pg, pm = pack._scaled(p, scale)
            sc, l = _masked_score(g, pg, pm)
            if sc > bs:
                bs, bloc = sc, l
        if bs >= GEOMETRY_MIN_SCORE and bloc is not None:
            positions.append(bloc)
    if positions:
        pg0, _ = pack._scaled(main_patches[0], scale)
        win = pack._set_window_around(None, positions, pg0.shape, shape)
        if win is not None:
            pack.search_region = win
    for p in pack.patches:
        if p["roi"] in ("playpause", "speed"):
            p["gray"], p["mask"] = pack._scaled(p, scale)

    # combat 组：同样缩放，窗口收窄到标签的中位匹配位置附近
    if combat_patches:
        cpos = []
        for g in sample_grays:
            bs, bloc = -1.0, None
            for p in combat_patches:
                pg, pm = pack._scaled(p, scale)
                sc, l = _masked_score(g, pg, pm)
                if sc > bs:
                    bs, bloc = sc, l
            if bs >= combat_patches[0]["thresh"] and bloc is not None:
                cpos.append(bloc)
        if cpos:
            pg0, _ = pack._scaled(combat_patches[0], scale)
            win = pack._set_window_around(None, cpos, pg0.shape, shape)
            if win is not None:
                pack.combat_region = win
        for p in combat_patches:
            p["gray"], p["mask"] = pack._scaled(p, scale)

    pack.geometry = {"scale": scale, "score": round(med, 3), "warn": warn}
    print(f"[nc-engine] 几何自适应: scale={scale} score={med:.2f} "
          f"scales={scale_scores} warn={warn}", flush=True)
    return pack.geometry


def classify(gray: np.ndarray, pack: TemplatePack) -> int:
    """对一张处理分辨率灰度帧做状态判定（限定区域全帧搜索）。"""
    return classify_full(gray, pack)[0]


def classify_full(gray: np.ndarray, pack: TemplatePack) -> tuple:
    """状态判定 + 作战画面判定。返回 (state, in_combat)。"""
    x0, y0, x1, y1 = pack.search_window(gray.shape)
    region = gray[y0:y1, x0:x1]

    best = {"playpause": None, "speed": None}  # group -> (patch, score)
    for p in pack.patches:
        if p["roi"] == "combat":
            continue
        s, _ = _masked_score(region, p["gray"], p["mask"])
        if s < p["thresh"]:
            continue
        cur = best.get(p["roi"])
        if cur is None or s > cur[1]:
            best[p["roi"]] = (p, s)

    in_combat = False
    if pack.has_combat:
        cx0, cy0, cx1, cy1 = pack.combat_window(gray.shape)
        cregion = gray[cy0:cy1, cx0:cx1]
        for p in pack.patches:
            if p["roi"] != "combat":
                continue
            s, _ = _masked_score(cregion, p["gray"], p["mask"])
            if s >= p["thresh"]:
                in_combat = True
                break

    right = best["playpause"]
    if right is None:
        return STATE_NORMAL, in_combat
    if right[0]["state"] == "paused":
        return STATE_PAUSED, in_combat
    # running(❚❚) 命中后看速度键
    left = best["speed"]
    if left is None:
        return STATE_NORMAL, in_combat
    if left[0]["state"] == "speed1x":
        return STATE_SPEED_1X, in_combat
    if left[0]["state"] == "speed2x":
        return STATE_SPEED_2X, in_combat
    return STATE_NORMAL, in_combat


def make_classifier(pack: TemplatePack):
    """生成多线程 worker 用的闭包。"""
    def work(frame_bgr: np.ndarray):
        g = preprocess(frame_bgr, pack.proc_size)
        return classify_full(g, pack), g
    return work
