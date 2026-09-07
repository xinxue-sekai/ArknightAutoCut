# pack_from_video.py —— 从一段视频直接校准模板包（推荐的新手校准方式）
#
# 原理：给出一局录像里"已知状态"的三个时间点（暂停中 / 运行中 / 2x 运行中），
# 再给出右上角两个按钮的像素框，脚本自动裁补丁、生成掩码、写 manifest，
# 并在已知状态帧上做分类自检。
#
# 像素框的测量方法（任选其一）：
#   1. 用任意截图/看图软件打开一帧暂停画面，读出按钮区域的像素坐标；
#   2. 或先用 --guess 让脚本按 16:9/16:10 常见位置给出参考框，再微调。
#
# 坐标格式（--boxes，原生像素，相对于视频分辨率）：
#   "speed=x0,y0,x1,y1;playpause=x0,y0,x1,y1"
#   也支持 0~1 归一化写法（数值均 <=1 时自动按归一化处理）。
#
# 用法示例：
#   uv run python tools/pack_from_video.py --video 一局录像.mp4 \
#       --paused 120 --running 80 --speed2x 80 \
#       --boxes "speed=1578,108,1698,215;playpause=1745,108,1868,215" \
#       --out ../newcut_service/template_pack --verify

import argparse
import json
import os
import sys

import cv2
import numpy as np

PROC_W = 640
MASK_THRESH = 130
THRESH = 0.75

# 常见全屏游戏录制的参考框（原生像素，16:9 1920x1080 基准；
# 其他分辨率按宽等比换算，仅作为 --guess 的起点，不保证精确）
GUESS_1080 = {
    "speed": (1575, 15, 1730, 140),
    "playpause": (1750, 15, 1915, 140),
}


def grab_frame(video, sec):
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise SystemExit(f"无法打开视频: {video}")
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(sec * cap.get(cv2.CAP_PROP_FPS)))
    ok, f = cap.read()
    cap.release()
    if not ok:
        raise SystemExit(f"无法读取 {sec}s 处的帧")
    return f


def parse_boxes(s, frame_w, frame_h):
    boxes = {}
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        name, coords = part.split("=", 1)
        vals = [float(v) for v in coords.split(",")]
        if len(vals) != 4:
            raise SystemExit(f"坐标格式错误: {part}")
        if all(v <= 1.0 for v in vals):  # 归一化写法
            vals = [vals[0] * frame_w, vals[1] * frame_h,
                    vals[2] * frame_w, vals[3] * frame_h]
        boxes[name.strip()] = tuple(int(round(v)) for v in vals)
    return boxes


def guess_boxes(frame_w, frame_h):
    sx, sy = frame_w / 1920.0, frame_h / 1080.0
    return {k: (int(v[0] * sx), int(v[1] * sy), int(v[2] * sx), int(v[3] * sy))
            for k, v in GUESS_1080.items()}


def crop_patch(frame, box, proc_w):
    x0, y0, x1, y1 = box
    crop = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    scale = proc_w / frame.shape[1]
    nw, nh = max(1, int(round(crop.shape[1] * scale))), max(1, int(round(crop.shape[0] * scale)))
    return cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_AREA)


def main():
    ap = argparse.ArgumentParser(description="从视频校准 NewCut 模板包")
    ap.add_argument("--video", required=True, help="一局录像（任意分辨率）")
    ap.add_argument("--paused", type=float, required=True, help="暂停中画面的秒数")
    ap.add_argument("--running", type=float, required=True, help="运行中（1x 或 2x）画面的秒数")
    ap.add_argument("--speed1x", type=float, default=None, help="1x 速度画面的秒数（可选）")
    ap.add_argument("--speed2x", type=float, default=None, help="2x 速度画面的秒数（可选）")
    ap.add_argument("--boxes", required=True,
                    help='按钮像素框 "speed=x0,y0,x1,y1;playpause=x0,y0,x1,y1"（支持 0~1 归一化）')
    ap.add_argument("--guess", action="store_true", help="按 1080p 全屏基准换算参考框（需再人工微调）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=PROC_W, help="处理分辨率宽（默认 640）")
    ap.add_argument("--verify", action="store_true", help="生成后在已知状态帧上自检")
    args = ap.parse_args()

    paused_f = grab_frame(args.video, args.paused)
    h, w = paused_f.shape[:2]
    boxes = parse_boxes(args.boxes, w, h) if args.boxes else guess_boxes(w, h)

    running_f = grab_frame(args.video, args.running)
    proc_w = args.width
    proc_h = int(round(proc_w * h / w))

    os.makedirs(args.out, exist_ok=True)
    patches = []
    for fname, state, group, sec, box in [
        ("paused.png", "paused", "playpause", args.paused, boxes.get("playpause")),
        ("running.png", "running", "playpause", args.running, boxes.get("playpause")),
        ("speed1x.png", "speed1x", "speed", args.speed1x, boxes.get("speed")),
        ("speed2x.png", "speed2x", "speed", args.speed2x, boxes.get("speed")),
    ]:
        if sec is None or box is None:
            continue
        f = paused_f if sec == args.paused else running_f
        patch = crop_patch(f, box, proc_w)
        cv2.imwrite(os.path.join(args.out, fname), patch)
        patches.append({"name": state, "state": state, "roi": group,
                        "thresh": THRESH, "mask_thresh": MASK_THRESH, "file": fname})
        print(f"[pack] {fname}: {patch.shape[1]}x{patch.shape[0]} state={state}")

    manifest = {
        "version": 2,
        "proc_size": [proc_w, proc_h],
        "rois": {k: [round(v[0] / w, 4), round(v[1] / h, 4),
                     round(v[2] / w, 4), round(v[3] / h, 4)] for k, v in boxes.items()},
        "search_region": [0.35, 0.0, 1.0, 0.55],
        "patches": patches,
    }
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[pack] 模板包已生成: {os.path.abspath(args.out)} (proc {proc_w}x{proc_h})")

    if args.verify:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "newcut_service"))
        from nc_match import TemplatePack, preprocess, classify
        pack = TemplatePack(args.out)
        print("[verify] 已知状态帧分类自检:")
        name_map = {0: "NORMAL", 1: "PAUSED", 2: "SPEED_1X", 3: "SPEED_2X"}
        for sec, expect in [(args.paused, "PAUSED"),
                            (args.running, "SPEED_1X" if args.speed1x == args.running
                             else ("SPEED_2X" if args.speed2x == args.running else "NORMAL"))]:
            f = grab_frame(args.video, sec)
            st = classify(preprocess(f, pack.proc_size), pack)
            mark = "✔" if name_map[st] == expect else f"(期望 {expect})"
            print(f"  t={sec}s -> {name_map[st]} {mark if mark != '✔' else '✔'}")


if __name__ == "__main__":
    main()
