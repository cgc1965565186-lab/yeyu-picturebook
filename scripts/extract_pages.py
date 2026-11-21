#!/usr/bin/env python3
"""把绘本 PDF 栅格化成逐页 PNG，供视觉理解与图生视频首帧使用。

用法：
    python3 scripts/extract_pages.py --pdf 素材/绘本素材/中文绘本01.pdf --out pages/CN01 --dpi 150
输出：
    pages/CN01/p01.png ... pNN.png（同时写 manifest.json：页数、尺寸、sha256）
"""
import argparse, hashlib, json, os, subprocess, sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dpi", type=int, default=150)
    a = ap.parse_args()

    pdf, out = Path(a.pdf), Path(a.out)
    if not pdf.is_file():
        sys.exit(f"PDF 不存在：{pdf}")
    out.mkdir(parents=True, exist_ok=True)
    prefix = out / "p"
    subprocess.run(["pdftoppm", "-png", "-r", str(a.dpi), str(pdf), str(prefix)], check=True)
    pages = sorted(out.glob("p*.png"))
    items = []
    for p in pages:
        items.append({"page": p.name, "bytes": p.stat().st_size,
                      "sha256": hashlib.sha256(p.read_bytes()).hexdigest()[:16]})
    (out / "manifest.json").write_text(json.dumps({"pdf": str(pdf), "dpi": a.dpi,
                                                   "pages": len(items), "items": items},
                                                  ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{pdf.name} → {out}：{len(items)} 页 @ {a.dpi}dpi")


if __name__ == "__main__":
    main()
