# build_portable.py —— 从源码组装自包含 CEP 插件（发布/安装用）
#
# 做四件事：
#   1. 下载 Windows 嵌入式 Python（3.12）解压到插件 runtime/
#   2. 安装 numpy / opencv-python / imageio-ffmpeg（cp312 轮子）
#   3. 校验/补齐引擎与面板文件
#   4. 可选：从一段录像直接校准模板包（--calibrate-* 参数）
#
# 依赖：本机需有 uv（https://docs.astral.sh/uv/）。仅支持 Windows。
#
# 用法（仓库根目录）：
#   uv run python service/tools/build_portable.py
#   uv run python service/tools/build_portable.py \
#       --calibrate-video 一局录像.mp4 --paused 120 --running 80 --speed2x 80 \
#       --boxes "speed=1578,108,1698,215;playpause=1745,108,1868,215"

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
ENGINE_SRC = os.path.join(REPO, "service", "newcut_service")
TOOLS_SRC = os.path.join(REPO, "service", "tools")
PANEL_SRC = os.path.join(REPO, "cep-plugin", "com.newcut.arknights")

PY_VERSION = "3.12.8"
PY_EMBED_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
PY_PTH_NAME = f"python{PY_VERSION.rpartition('.')[0].replace('.', '')}._pth"
PTH_CONTENT = "python312.zip\n.\nLib\\site-packages\nimport site\n"

ENGINE_FILES = ["server.py", "pipeline.py", "decode.py", "nc_match.py",
                "nc_segment.py", "nc_states.py"]
TOOL_FILES = ["calibrate.py", "pack_from_video.py", "batch_process.py"]
PANEL_FILES = {"index.html": "", "CSXS/manifest.xml": "",
               "js/CSInterface.js": "", "js/main.js": "", "js/bridge-cep.js": "",
               "js/service-manager.js": "", "jsx/host.jsx": "",
               "！一键安装.bat": ""}


def download(url, dst):
    print(f"[build] 下载 {url}")
    with urllib.request.urlopen(url, timeout=120) as r, open(dst, "wb") as f:
        shutil.copyfileobj(r, f)


def run_uv_pip(target, pkgs):
    cmd = ["uv", "pip", "install", "--target", target,
           "--python-version", "3.12"] + pkgs
    print("[build] " + " ".join(cmd))
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="组装自包含 CEP 插件")
    ap.add_argument("--out", default=PANEL_SRC)
    ap.add_argument("--skip-python", action="store_true", help="跳过 Python 下载安装（增量更新引擎/面板时用）")
    ap.add_argument("--calibrate-video", default=None)
    ap.add_argument("--paused", type=float, default=None)
    ap.add_argument("--running", type=float, default=None)
    ap.add_argument("--speed2x", type=float, default=None)
    ap.add_argument("--boxes", default=None)
    args = ap.parse_args()
    target_dir = os.path.join(args.out, "runtime")

    if sys.platform != "win32":
        raise SystemExit("仅支持 Windows（构建目标为 Windows CEP 插件）")

    # 1) 嵌入式 Python
    if not args.skip_python:
        os.makedirs(target_dir, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            zpath = os.path.join(td, "py.zip")
            download(PY_EMBED_URL, zpath)
            with zipfile.ZipFile(zpath) as z:
                z.extractall(target_dir)
        with open(os.path.join(target_dir, PY_PTH_NAME), "w") as f:
            f.write(PTH_CONTENT)
        print(f"[build] Python {PY_VERSION} 就绪")

    # 2) 依赖
    if not args.skip_python:
        run_uv_pip(os.path.join(target_dir, "Lib", "site-packages"),
                   ["numpy", "opencv-python", "imageio-ffmpeg"])

    # 3) 引擎 / 工具 / 面板文件
    os.makedirs(os.path.join(args.out, "engine"), exist_ok=True)
    for fname in ENGINE_FILES:
        shutil.copy2(os.path.join(ENGINE_SRC, fname), os.path.join(args.out, "engine", fname))
    for fname in TOOL_FILES:
        srcf = os.path.join(TOOLS_SRC, fname)
        if os.path.exists(srcf):
            shutil.copy2(srcf, os.path.join(args.out, "engine", fname))
    for rel in PANEL_FILES:
        srcf = os.path.join(PANEL_SRC, rel)
        dstf = os.path.join(args.out, rel)
        os.makedirs(os.path.dirname(dstf) or args.out, exist_ok=True)
        if os.path.exists(srcf):
            shutil.copy2(srcf, dstf)
        else:
            print(f"[build][警告] 面板文件缺失: {rel}")
    print("[build] 引擎与面板文件已就绪")

    # 4) 模板包：已存在则保留；否则可从录像校准
    pack_dir = os.path.join(args.out, "engine", "template_pack")
    if os.path.exists(os.path.join(pack_dir, "manifest.json")):
        print("[build] 模板包已存在，保留")
    elif args.calibrate_video:
        cmd = [os.path.join(target_dir, "python.exe"),
               os.path.join(args.out, "engine", "pack_from_video.py"),
               "--video", args.calibrate_video,
               "--out", pack_dir, "--verify"]
        for flag, key in [("--paused", "paused"), ("--running", "running"),
                          ("--speed2x", "speed2x"), ("--boxes", "boxes")]:
            v = getattr(args, key)
            if v is not None:
                cmd += [flag, str(v)]
        subprocess.run(cmd, check=True)
    else:
        print("[build][提示] 尚无模板包。使用前请运行（二选一）：\n"
              "  a) build_portable.py --calibrate-video 一局录像.mp4 --paused .. --running .. --boxes ..\n"
              "  b) 手动：engine/pack_from_video.py 或 engine/calibrate.py")

    # 自检
    checks = [os.path.join(target_dir, "pythonw.exe"),
              os.path.join(args.out, "CSXS", "manifest.xml"),
              os.path.join(args.out, "index.html"),
              os.path.join(args.out, "jsx", "host.jsx")]
    missing = [c for c in checks if not os.path.exists(c)]
    if missing:
        raise SystemExit(f"[build] 缺少文件: {missing}")
    print("[build] 完成！把整个插件文件夹复制到 "
          r"%APPDATA%\Adobe\CEP\extensions\ 后重启 PR。")
