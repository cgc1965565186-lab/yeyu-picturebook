#!/usr/bin/env python3
"""按镜头清单批量提交 MiniMax 生成任务，然后统一挂机等待。

清单格式见 shots/cn01_c3.json：{"shots":[{"id","seconds","prompt","scene"}]}
首帧：给了 --frames <dir> 且存在 <dir>/<id>.png 时，自动改用图生视频。

用法：
    python3 scripts/run_shots.py --shots shots/cn01_c3.json --dry-run
    python3 scripts/run_shots.py --shots shots/cn01_c3.json --max-cny 60
    python3 scripts/run_shots.py --shots shots/cn01_c3.json --only CN01-S03 --frames pages/CN01
"""
import argparse, json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "minimax_video.py"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", required=True)
    ap.add_argument("--model", default="MiniMax-H3-Lite")
    ap.add_argument("--resolution", default="480P")
    ap.add_argument("--max-cny", type=float, default=None)
    ap.add_argument("--only", nargs="*", default=None, help="只跑指定 id")
    ap.add_argument("--frames", default=None, help="首帧图目录：<dir>/<id>.png")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-watch", action="store_true")
    ap.add_argument("--tag", default=None, help="业务 ID 后缀，用于对同一镜头重新生成（如 v2）")
    a = ap.parse_args()

    if not a.dry_run:
        probe = subprocess.run([sys.executable, "-c",
                                "import sys;sys.path.insert(0,'scripts');import minimax_video as m;"
                                "sys.exit(0 if m.KEY else 1)"], cwd=ROOT)
        if probe.returncode != 0:
            sys.exit("没有可用的 MiniMax 密钥：写入 .secrets/minimax.key 后重试（本次未产生任何费用）")

    spec = json.loads(Path(a.shots).read_text(encoding="utf-8"))
    shots = spec["shots"]
    if a.only:
        shots = [s for s in shots if s["id"] in a.only]
        if not shots:
            sys.exit(f"没有匹配的镜头：{a.only}")

    total = sum(int(s["seconds"]) for s in shots)
    print(f"{spec.get('_book', a.shots)}：{len(shots)} 个镜头，合计 {total} 秒，"
          f"{a.model} {a.resolution}")

    ok = 0
    for s in shots:
        bid = f"{s['id']}-{a.tag}" if a.tag else s["id"]
        cmd = [sys.executable, str(CLI), "submit", "--business-id", bid,
               "--prompt", s["prompt"], "--duration", str(s["seconds"]),
               "--model", a.model, "--resolution", a.resolution]
        frame = None
        if a.frames:
            d = Path(a.frames)
            for cand in (d / f"{s['id']}.png", d / f"p-{int(s['page']):02d}.png" if s.get("page") else None):
                if cand and cand.is_file():
                    frame = cand
                    break
        if frame and frame.is_file():
            cmd += ["--first-frame", str(frame)]
        if a.dry_run:
            cmd.append("--dry-run")
        if a.max_cny:
            cmd += ["--max-cny", str(a.max_cny)]
        print(f"\n── {s['id']}  {s.get('scene', '')}  {s['seconds']}s")
        r = subprocess.run(cmd)
        if r.returncode == 0:
            ok += 1
        elif not a.dry_run:
            print(f"!! {s['id']} 提交失败，继续下一个", file=sys.stderr)

    if a.dry_run or a.no_watch:
        return
    if ok == 0:
        sys.exit("全部提交失败，跳过挂机（没有产生费用）")
    print("\n══ 统一挂机等待")
    subprocess.run([sys.executable, str(CLI), "watch", "--interval", "15",
                    "--deadline-hours", "2"] + (["--max-cny", str(a.max_cny)] if a.max_cny else []))


if __name__ == "__main__":
    main()
