# calibrate.py —— NewCut 模板包校准工具
#
# 从你自己的游戏基准截图裁剪出右上角控制条的状态补丁，生成模板包
# （manifest.json + 补丁 PNG）。分析服务用这个包做逐帧状态判定。
#
# 需要的截图（游戏内战斗界面、任意分辨率、16:9）：
#   --paused  暂停中的画面（右上角是 ▶）
#   --run1x   运行中 1 倍速画面（右上角是 ❚❚，速度键显示 1X）
#   --run2x   运行中 2 倍速画面（速度键显示 2X）（可选）
#
# 用法示例：
#   uv run python tools/calibrate.py \
#       --paused paused.png --run1x run1x.png --run2x run2x.png \
#       --out ../newcut_service/template_pack
#
# --verify：生成后交叉验证——每个补丁回匹配各截图的 ROI，
#           应看到"自家截图 ≈1.0、异状态截图明显更低"。

import argparse
import json
import os
import sys

import cv2

_HERE = os.path.dirname(os.path.abspath(__file__))
# service 布局: tools/../newcut_service；CEP 引擎布局: 与本文件同目录
for _cand in (os.path.join(_HERE, "..", "newcut_service"), _HERE):
    if os.path.isfile(os.path.join(_cand, "nc_match.py")):
        sys.path.insert(0, _cand)
        break
from nc_match import DEFAULT_ROIS, MANIFEST_NAME  # noqa: E402

PROC_W, PROC_H = 480, 270

# 补丁定义：名称 -> (来源截图, ROI, 状态名, 默认阈值)
PATCH_SPECS = [
    ("paused.png",  "paused",  "paused",   "playpause", 0.75),
    ("running.png", "running", "running",  "playpause", 0.75),
    ("speed1x.png", "speed1x", "speed_1x", "speed",     0.75),
    ("speed2x.png", "speed2x", "speed_2x", "speed",     0.75),
]
# manifest 中 state 字段用 patch 名（供 pipeline 阈值覆盖键对应）
STATE_KEY = {"paused.png": "paused", "running.png": "running",
             "speed1x.png": "speed1x", "speed2x.png": "speed2x"}


def load_shot(path, proc_size):
    img = cv2.imread(path)
    if img is None:
        raise SystemExit(f"无法读取截图: {path}")
    return cv2.cvtColor(cv2.resize(img, proc_size, interpolation=cv2.INTER_AREA),
                        cv2.COLOR_BGR2GRAY)


def roi_crop(gray, roi_norm):
    h, w = gray.shape[:2]
    x0 = int(roi_norm[0] * w); y0 = int(roi_norm[1] * h)
    x1 = max(x0 + 1, int(roi_norm[2] * w)); y1 = max(y0 + 1, int(roi_norm[3] * h))
    return gray[y0:y1, x0:x1]


def main():
    ap = argparse.ArgumentParser(description="NewCut 模板包校准")
    ap.add_argument("--paused", help="暂停中截图（右上角 ▶）")
    ap.add_argument("--run1x", help="运行中 1x 截图（右上角 ❚❚）")
    ap.add_argument("--run2x", help="运行中 2x 截图（可选）")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "newcut_service", "template_pack"))
    ap.add_argument("--width", type=int, default=PROC_W, help="处理分辨率宽（默认 480）")
    ap.add_argument("--verify", action="store_true", help="生成后交叉验证分数矩阵")
    args = ap.parse_args()

    if not args.paused or not args.run1x:
        raise SystemExit("至少需要 --paused 和 --run1x 两张截图")

    proc_size = (args.width, round(args.width * 9 / 16))
    shots = {"paused": load_shot(args.paused, proc_size),
             "running": load_shot(args.run1x, proc_size)}
    if args.run2x:
        shots["run2x"] = load_shot(args.run2x, proc_size)

    src_for = {"paused": shots["paused"], "running": shots["running"],
               "speed1x": shots["running"], "speed2x": shots.get("run2x", shots["running"])}

    os.makedirs(args.out, exist_ok=True)
    patches = []
    for fname, pname, state, roi, thresh in PATCH_SPECS:
        if pname == "speed2x" and not args.run2x:
            continue
        crop = roi_crop(src_for[pname], DEFAULT_ROIS[roi])
        cv2.imwrite(os.path.join(args.out, fname), crop)
        patches.append({"name": pname, "state": STATE_KEY[fname], "roi": roi,
                        "thresh": thresh, "file": fname})
        print(f"[calibrate] 写出补丁 {fname} ({crop.shape[1]}x{crop.shape[0]}) "
              f"roi={roi} state={STATE_KEY[fname]}")

    manifest = {
        "version": 1,
        "proc_size": list(proc_size),
        "rois": DEFAULT_ROIS,
        "patches": patches,
    }
    with open(os.path.join(args.out, MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[calibrate] 模板包已生成: {os.path.abspath(args.out)}")

    if args.verify:
        print("\n[verify] 交叉分数矩阵（行=补丁, 列=截图; 应呈现对角高、非对角低）:")
        cols = list(shots.keys())
        head = "          " + "".join(f"{c:>10s}" for c in cols)
        print(head)
        for p in patches:
            row = f"{p['name']:<10s}"
            for c in cols:
                s = cv2.matchTemplate(roi_crop(shots[c], DEFAULT_ROIS[p['roi']]),
                                      cv2.imread(os.path.join(args.out, p["file"]), 0),
                                      cv2.TM_CCOEFF_NORMED)
                row += f"{float(s.max()):>10.3f}"
            print(row)


if __name__ == "__main__":
    main()
