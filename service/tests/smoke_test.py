# smoke_test.py —— NewCut 自研引擎冒烟测试
#
# 全链路：模板校准（fixtures 截图 -> 模板包）
#        -> 用 fixture 截图合成测试视频（暂停30帧/1x30帧/运动30帧）
#        -> 启动服务 -> probe -> analyze -> 校验方案 JSON
#
# 运行: uv run python tests/smoke_test.py

import json
import os
import subprocess
import sys
import time
import urllib.request

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
OUT = os.path.join(ROOT, "tests", "_out")
PACK = os.path.join(OUT, "pack")
PORT = 8899
BASE = f"http://127.0.0.1:{PORT}"


def http(method, path, body=None, timeout=30):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def calibrate():
    cmd = [sys.executable, os.path.join(ROOT, "tools", "calibrate.py"),
           "--paused", os.path.join(FIXTURES, "paused.png"),
           "--run1x", os.path.join(FIXTURES, "run1x.png"),
           "--run2x", os.path.join(FIXTURES, "run2x.png"),
           "--out", PACK, "--verify"]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
    print(r.stdout)
    assert r.returncode == 0, f"校准失败: {r.stderr}"


def build_test_video(path):
    """暂停界面30帧 + 1x界面30帧 + 运动方块30帧，30fps 640x360。"""
    paused = cv2.imread(os.path.join(FIXTURES, "paused.png"))
    run1x = cv2.imread(os.path.join(FIXTURES, "run1x.png"))
    paused_f = cv2.resize(paused, (640, 360), interpolation=cv2.INTER_AREA)
    run1x_f = cv2.resize(run1x, (640, 360), interpolation=cv2.INTER_AREA)

    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (640, 360))
    assert vw.isOpened()
    for _ in range(30):
        vw.write(paused_f)
    for _ in range(30):
        vw.write(run1x_f)
    for i in range(30):
        frame = np.full((360, 640, 3), 40, dtype=np.uint8)
        x = 50 + i * 15
        cv2.rectangle(frame, (x, 150), (x + 60, 210), (60, 200, 90), -1)
        vw.write(frame)
    vw.release()


def main():
    os.makedirs(OUT, exist_ok=True)

    print("=" * 60)
    print("[1] 模板校准")
    calibrate()
    assert os.path.isfile(os.path.join(PACK, "manifest.json"))

    print("[2] 合成测试视频")
    video = os.path.join(OUT, "smoke_video.mp4")
    build_test_video(video)

    print("[3] 启动服务")
    server = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "newcut_service", "server.py"),
         "--port", str(PORT)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(30):
            try:
                st = http("GET", "/status", timeout=3)
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("服务未在 30s 内就绪")
        assert st["ok"]
        print(f"    /status OK: v{st['version']}")

        probe = http("POST", "/probe", {"video_path": video})
        print(f"[4] /probe OK: {probe['frame_count']}帧 @ {probe['fps']:.2f}fps")
        assert probe["frame_count"] == 90

        job = http("POST", "/analyze", {
            "video_path": video,
            "params": {"template_dir": PACK},
        })
        jid = job["job_id"]
        print(f"[5] /analyze OK: job={jid}")
        t0 = time.time()
        while True:
            j = http("GET", f"/job/{jid}")
            if j["state"] != "running":
                break
            if time.time() - t0 > 180:
                raise RuntimeError("分析超时")
            time.sleep(1)
        assert j["state"] == "done", f"分析失败: {j.get('error')}"
        r = j["result"]

        # ---- 结构校验 ----
        assert r["fps"] > 0 and r["frame_count"] == 90
        assert isinstance(r["keep_ranges"], list) and r["keep_ranges"]
        kept = sum(e - s for s, e in r["keep_ranges"])
        assert kept == r["total_kept_frames"]
        for a, b in zip(r["keep_ranges"], r["keep_ranges"][1:]):
            assert a[1] <= b[0], f"区间重叠: {a} {b}"
        cov = kept + sum(e - s for s, e in r["delete_ranges"])
        assert cov == 90, f"区间覆盖不完全: {cov}"

        # ---- 行为校验（fixture 视频的预期剪辑结果）----
        # 帧[0,30) 暂停界面静止: 删中段 [3,27) 留首尾缓冲
        # 帧[30,60) 1x 界面: 倍速段标记
        # 帧[60,90) 运动画面: 正常保留
        assert r["speeds"] and r["speeds"][0]["type"] == "1x" \
            and r["speeds"][0]["start"] == 30 and r["speeds"][0]["end"] == 59, r["speeds"]
        assert r["pauses"] and r["pauses"][0]["start"] == 0 and r["pauses"][0]["end"] == 29
        expected_keep = [[0, 3], [27, 90]]
        assert r["keep_ranges"] == expected_keep, \
            f"keep_ranges {r['keep_ranges']} != {expected_keep}"

        print(f"[6] 分析结果: 暂停段={len(r['pauses'])} 倍速段={len(r['speeds'])} "
              f"保留={r['keep_ranges']} 删除{r['deleted_frame_count']}帧 "
              f"{r['duration_sec']:.1f}s -> {r['edited_duration_sec']:.1f}s")
        print("[7] 冒烟测试全部通过 ✔")
    finally:
        server.terminate()
        try:
            out = server.communicate(timeout=5)[0]
            print("---- 服务日志 ----")
            print(out[-800:] if out else "(无输出)")
        except Exception:
            server.kill()


if __name__ == "__main__":
    main()
