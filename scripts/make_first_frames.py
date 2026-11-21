#!/usr/bin/env python3
"""从逐页图裁出图生视频用的首帧，统一成 1.8:1（MiniMax 480P/2K 的实际输出比例）。

为什么必须 1.8:1：图生视频的 ratio 恒为 adaptive，输出宽高比由首帧决定；
首帧若给 2:1 的整幅展开页，成片就是 2:1，和交付规格对不上。

用法：
    python3 scripts/make_first_frames.py --crops shots/cn01_crops.json --pages pages/CN01 --out frames/CN01
"""
import argparse, json
from pathlib import Path
from PIL import Image

RATIO = 16 / 9      # H3 2K 实测输出 2560x1440=16:9；480P 是 864x480=1.8，两者不同
WIDTH = 1800       # 留足像素给 2K 生成


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crops", required=True)
    ap.add_argument("--pages", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    crops = json.loads(Path(a.crops).read_text(encoding="utf-8"))
    pages, out = Path(a.pages), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    for shot_id, c in crops.items():
        src = pages / f"p-{int(c['page']):02d}.png"
        if not src.is_file():
            print(f"跳过 {shot_id}：缺 {src.name}")
            continue
        with Image.open(src) as im:
            im = im.convert("RGB")
            w, h = im.size
            x1, y1, x2, y2 = c["box"]
            region = im.crop((round(x1 * w), round(y1 * h), round(x2 * w), round(y2 * h)))
            height = round(WIDTH / RATIO)
            mode = c.get("mode", "pad")
            if mode == "crop":
                rw, rh = region.size
                if rw / rh > RATIO:
                    nw = round(rh * RATIO); region = region.crop(((rw - nw) // 2, 0, (rw - nw) // 2 + nw, rh))
                else:
                    nh = round(rw / RATIO); region = region.crop((0, (rh - nh) // 2, rw, (rh - nh) // 2 + nh))
                region = region.resize((WIDTH, height), Image.LANCZOS)
            else:
                # pad：整幅保留，等比缩进 1.8:1 画布，四周补白（原书本来就有白边）
                rw, rh = region.size
                scale = min(WIDTH / rw, height / rh)
                region = region.resize((max(1, round(rw * scale)), max(1, round(rh * scale))), Image.LANCZOS)
                canvas = Image.new("RGB", (WIDTH, height), "white")
                canvas.paste(region, ((WIDTH - region.width) // 2, (height - region.height) // 2))
                region = canvas
            dst = out / f"{shot_id}.png"
            region.save(dst)
            print(f"{shot_id} ← {src.name} {c.get('note', '')} → {dst.name} {region.size}")


if __name__ == "__main__":
    main()
