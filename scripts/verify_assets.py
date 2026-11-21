#!/usr/bin/env python3
"""按 清单/素材清单.csv 校验素材完整性（路径 + SHA-256）。

用法：
    python3 scripts/verify_assets.py                      # 默认以本脚本上一级为根
    python3 scripts/verify_assets.py --root docs/plan     # 指定 清单/ 与 素材/ 所在目录
退出码：0 全部通过；1 有缺失或哈希不符。
"""
import csv, hashlib, os, sys

import argparse

DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT, help="包含 清单/ 与 素材/ 的目录")
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    manifest = os.path.join(root, "清单", "素材清单.csv")
    rows = list(csv.DictReader(open(manifest, encoding="utf-8-sig")))
    ok = missing = mismatch = 0
    for r in rows:
        p = os.path.join(root, r["相对路径"])
        if not os.path.exists(p):
            print(f"MISSING  {r['相对路径']}")
            missing += 1
            continue
        if sha256(p) != r["sha256"]:
            print(f"MISMATCH {r['相对路径']}")
            mismatch += 1
            continue
        ok += 1
    print(f"\n{ok}/{len(rows)} 通过，缺失 {missing}，哈希不符 {mismatch}")
    return 0 if missing == 0 and mismatch == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
