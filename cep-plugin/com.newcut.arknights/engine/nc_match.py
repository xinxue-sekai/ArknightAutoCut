# nc_match.py —— NewCut 自研帧状态匹配器 v2
#
# v1 的教训（素材 1920x1200 / 带窗口标题栏 / 16:10 导致 ROI 全部落空）：
#   匹配不再假设 UI 位置。参考原项目 arknight-auto-editing 的两个关键设计：
#     1) 掩码 NCC：模板按亮度阈值二值化，只匹配亮色图标像素（抗底板/背景干扰）
#     2) 搜索窗口放宽（原项目为 2 倍 ROI；v2 直接在限定区域内全帧搜索）
#   并改为等比缩放（黑边填充），杜绝 16:10 / 16:9 / 不同窗口布局造成的形变。
#
# 判定逻辑：
#   playpause 组（右键）最佳匹配 paused(▶) 且过阈值      -> PAUSED
#   playpause 组最佳匹配 running(❚❚) 且过阈值            -> 看速度组：
#       speed 组匹配 speed1x -> SPEED_1X；speed2x -> SPEED_2X；否则 NORMAL
#   无任何过阈值匹配                                      -> NORMAL

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

    def search_window(self, shape):
        """归一化搜索区域 -> 像素裁剪框（按帧尺寸缓存）。"""
        key = shape
        if key not in self._region_cache:
            h, w = shape[:2]
            x0 = int(self.search_region[0] * w); y0 = int(self.search_region[1] * h)
            x1 = max(x0 + 1, int(self.search_region[2] * w))
            y1 = max(y0 + 1, int(self.search_region[3] * h))
            self._region_cache[key] = (x0, y0, x1, y1)
        return self._region_cache[key]


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


def _masked_score(region: np.ndarray, patch_gray: np.ndarray, patch_mask: np.ndarray) -> float:
    """掩码 NCC：只对模板亮像素计算归一化互相关，返回区域内最大分。"""
    if region.shape[0] < patch_gray.shape[0] or region.shape[1] < patch_gray.shape[1]:
        return -1.0
    res = cv2.matchTemplate(region, patch_gray, cv2.TM_CCOEFF_NORMED, mask=patch_mask)
    res = res[np.isfinite(res)]
    if res.size == 0:
        return -1.0
    return float(res.max())


def classify(gray: np.ndarray, pack: TemplatePack) -> int:
    """对一张处理分辨率灰度帧做状态判定（限定区域全帧搜索）。"""
    x0, y0, x1, y1 = pack.search_window(gray.shape)
    region = gray[y0:y1, x0:x1]

    best = {"playpause": None, "speed": None}  # group -> (patch, score)
    for p in pack.patches:
        s = _masked_score(region, p["gray"], p["mask"])
        if s < p["thresh"]:
            continue
        cur = best.get(p["roi"])
        if cur is None or s > cur[1]:
            best[p["roi"]] = (p, s)

    right = best["playpause"]
    if right is None:
        return STATE_NORMAL
    if right[0]["state"] == "paused":
        return STATE_PAUSED
    # running(❚❚) 命中后看速度键
    left = best["speed"]
    if left is None:
        return STATE_NORMAL
    if left[0]["state"] == "speed1x":
        return STATE_SPEED_1X
    if left[0]["state"] == "speed2x":
        return STATE_SPEED_2X
    return STATE_NORMAL


def make_classifier(pack: TemplatePack):
    """生成多线程 worker 用的闭包。"""
    def work(frame_bgr: np.ndarray):
        g = preprocess(frame_bgr, pack.proc_size)
        return classify(g, pack), g
    return work
