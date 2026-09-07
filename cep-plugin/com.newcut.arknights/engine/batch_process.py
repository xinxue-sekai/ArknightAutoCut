# batch_process.py —— 批量分析 CLI
#
# 对文件夹内所有视频跑 NewCut 分析，方案 JSON（<视频名>.newcut.json）
# 写在视频旁边。面板里点「导入方案文件」即可加载并应用到 PR。
#
# 用法：
#   uv run python tools/batch_process.py --folder "D:\OBSStudio" [--pattern "*.mp4"] [--fast]

import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "newcut_service"))
import pipeline  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="NewCut 批量分析")
    ap.add_argument("--folder", required=True)
    ap.add_argument("--pattern", default="*.mp4")
    ap.add_argument("--fast", action="store_true", help="快速模式（每 N 帧采样，边界精度略降）")
    ap.add_argument("--template-dir", default=None)
    args = ap.parse_args()

    videos = sorted(glob.glob(os.path.join(args.folder, args.pattern)))
    if not videos:
        raise SystemExit(f"未找到视频: {os.path.join(args.folder, args.pattern)}")

    ok = fail = 0
    for v in videos:
        out = os.path.splitext(v)[0] + ".newcut.json"
        if os.path.exists(out):
            print(f"[skip] {os.path.basename(v)} (已有方案)")
            continue
        print(f"[analyze] {os.path.basename(v)} ...", flush=True)
        t0 = time.time()
        try:
            params = {"fast_mode": args.fast}
            if args.template_dir:
                params["template_dir"] = args.template_dir
            result, _ = pipeline.run_analysis_full(v, params)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
            print(f"  -> {result['duration_sec']:.0f}s -> {result['edited_duration_sec']:.0f}s, "
                  f"暂停段 {len(result['pauses'])}, 用时 {time.time()-t0:.0f}s")
            ok += 1
        except Exception as e:
            print(f"  -> 失败: {e}")
            fail += 1
    print(f"完成: 成功 {ok}, 失败 {fail}")


if __name__ == "__main__":
    main()
