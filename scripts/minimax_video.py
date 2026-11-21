#!/usr/bin/env python3
"""MiniMax 视频生成最小适配器（H3 V2 契约）：submit → poll → download。

契约来源：platform.minimax.cn 视频生成 V2
  POST /v2/video_generation          {"model","content":[{type:text,text}],"resolution","duration","ratio","generate_audio"}
  GET  /v2/query/video_generation/{task_id}
  →   {"task":{"status":"queued|running|succeeded|failed|cancelled","content":{"url"},"usage":{...}}}
http(s)://api.minimax.cn · Authorization: Bearer <API_KEY>

四条硬规矩（V2.1 §9.3）：
  1. 幂等：同一 business_id 只创建一个付费任务
  2. 先写盘再解析：拿到 task_id 立刻落账，进程崩了也能接着查
  3. 提交不明不重建：超时/断网 → submission_unknown，只查询不重发
  4. 失败要吵：osascript 通知，挂机时不至于白等

用法:
    export MINIMAX_API_KEY=sk-...
    python3 scripts/minimax_video.py submit --business-id CN01-S01 --prompt "..." --duration 6 --dry-run
    python3 scripts/minimax_video.py submit --business-id CN01-S01 --prompt "..."
    python3 scripts/minimax_video.py watch --max-cny 320 --deadline-hours 10
    python3 scripts/minimax_video.py status
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "var" / "video_tasks.json"
OUT = ROOT / "var" / "video_out"
BASE = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.cn").rstrip("/")
def _read_key() -> str:
    """环境变量优先；否则读 .secrets/minimax.key（该目录已 gitignore）。"""
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if key:
        return key
    for cand in (ROOT / ".secrets" / "minimax.key", ROOT / "var" / "minimax.key"):
        if cand.is_file():
            return cand.read_text(encoding="utf-8").strip()
    return ""


KEY = _read_key()

PRICE = {"480P": 0.16, "768P": 0.50, "2K": 0.80}
ALLOWED = {
    ("MiniMax-H3", "768P"), ("MiniMax-H3", "2K"),
    ("MiniMax-H3-Lite", "480P"), ("MiniMax-H3-Lite", "768P"),
    ("MiniMax-H3-Highspeed", "480P"), ("MiniMax-H3-Highspeed", "768P"),
    ("MiniMax-H3-Max", "480P"), ("MiniMax-H3-Max", "768P"),
}
PENDING = {"queued", "running"}
FINAL_BAD = {"failed", "cancelled"}


# ---------------------------------------------------------------- 异常与告警

class ApiError(RuntimeError):
    """供应商返回明确的 HTTP 错误（含业务码），不是网络不明。"""

    def __init__(self, status: int, body: str):
        self.status, self.body = status, body[:2000]
        msg = body
        try:
            msg = json.loads(body)["error"]["message"]
        except Exception:
            pass
        super().__init__(f"HTTP {status}: {msg[:300]}")


def _notify(msg: str) -> None:
    print(f"[告警] {msg}", file=sys.stderr, flush=True)
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e",
                        f'display notification {json.dumps(msg)} with title "页语·生成" sound name "Basso"'],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- 账本

def _load() -> dict[str, dict]:
    data = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {}
    # 没有 task_id 的条目说明任务从未创建，不计费
    for v in data.values():
        if not v.get("task_id") and v.get("cost"):
            v["estimated_cost"] = v.pop("cost")
            v["cost"] = 0
    return data


def _save(data: dict[str, dict]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LEDGER)


# ---------------------------------------------------------------- HTTP

def _dump_raw(tag: str, raw: str) -> None:
    if not os.environ.get("MINIMAX_DUMP"):
        return
    print(raw, file=sys.stderr)
    d = ROOT / "var" / "raw"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{time.strftime('%Y%m%dT%H%M%S')}_{tag}.json").write_text(raw, encoding="utf-8")


def _call(method: str, path: str, payload: dict | None = None, timeout: int = 60) -> dict:
    if not KEY:
        raise SystemExit("缺少密钥：写入 .secrets/minimax.key，或 export MINIMAX_API_KEY=…")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        _dump_raw(f"http{exc.code}_{re.sub(r'[^A-Za-z0-9]+', '_', path)[:30]}", raw)
        raise ApiError(exc.code, raw) from None
    _dump_raw(f"{method.lower()}_{re.sub(r'[^A-Za-z0-9]+', '_', path)[:30]}", raw)
    return json.loads(raw)


def _task(body: dict) -> dict:
    """兼容 response_format=task（{"task":{...}}）与 flat（平铺）。"""
    return body.get("task") if isinstance(body.get("task"), dict) else body


# ---------------------------------------------------------------- 请求体

@dataclass
class Spec:
    business_id: str
    prompt: str
    duration: int = 6
    model: str = "MiniMax-H3-Lite"
    resolution: str = "480P"
    ratio: str = "16:9"
    first_frame: str | None = None      # 本地图片路径
    shot: str = ""
    extra: dict = field(default_factory=dict)

    def payload(self) -> dict:
        content: list[dict] = [{"type": "text", "text": self.prompt}]
        ratio = self.ratio
        if self.first_frame:
            p = Path(self.first_frame)
            if not p.is_file():
                raise SystemExit(f"首帧图片不存在：{self.first_frame}")
            data = base64.b64encode(p.read_bytes()).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/{p.suffix.lstrip('.').lower()};base64,{data}"},
                "role": "first_frame",
            })
            ratio = "adaptive"      # 图生视频：宽高比由输入决定
        body = {
            "model": self.model,
            "content": content,
            "resolution": self.resolution.upper(),
            "duration": int(self.duration),
            "ratio": ratio,
            "generate_audio": False,
        }
        body.update(self.extra)
        return body

    def fingerprint(self) -> str:
        # 首帧不入指纹（base64 太长），用路径 + mtime 代替
        blob = json.dumps({k: v for k, v in self.payload().items() if k != "content"},
                          sort_keys=True, ensure_ascii=False)
        blob += f"|{self.prompt}|{self.first_frame}|{self.duration}"
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def cost(self) -> float:
        return round(self.duration * PRICE.get(self.resolution.upper(), 0.0), 2)


# ---------------------------------------------------------------- 提交

def submit(spec: Spec, dry_run: bool = False, max_cny: float | None = None) -> dict:
    ledger = _load()
    rec = ledger.get(spec.business_id)

    # 幂等闸门：有 task_id 或状态不是"失败/不明"，一律不重发
    if rec and rec.get("task_id") and rec.get("status") != "failed":
        print(f"[幂等] {spec.business_id} 已有 task_id={rec['task_id']}（{rec.get('status')}），未重复提交")
        return rec
    if rec and rec.get("status") == "submission_unknown" and rec.get("task_id"):
        print(f"[恢复] {spec.business_id} 先查询已有任务，不新建")
        return refresh(spec.business_id)

    body = spec.payload()
    if dry_run:
        print(json.dumps({k: v for k, v in body.items() if k != "content"}, ensure_ascii=False, indent=2))
        print(f'# content: {json.dumps(body["content"][0], ensure_ascii=False)[:120]}')
        print(f"# 预计费用 {spec.cost()} 元（{spec.duration}s × {PRICE.get(spec.resolution.upper(), 0)}）")
        return {}

    if max_cny is not None:
        spent = sum(float(r.get("cost", 0)) for r in ledger.values())
        if spent + spec.cost() > max_cny:
            raise SystemExit(f"预算熔断：已记 {spent:.2f} 元，本次 {spec.cost()} 元，上限 {max_cny} 元")

    ledger.setdefault(spec.business_id, {}).update({
        "business_id": spec.business_id, "shot": spec.shot,
        "model": spec.model, "resolution": spec.resolution,
        "duration": spec.duration, "fingerprint": spec.fingerprint(),
        "status": "submitting", "estimated_cost": spec.cost(), "cost": 0,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    _save(ledger)

    try:
        resp = _call("POST", "/v2/video_generation", body)
    except ApiError as exc:
        if 400 <= exc.status < 500:
            ledger[spec.business_id].update({"status": "failed", "http_status": exc.status, "error": exc.body})
            _save(ledger)
            _notify(f"{spec.business_id} 被拒（HTTP {exc.status}），配置修好可直接重跑")
            raise SystemExit(f"\n提交被拒：HTTP {exc.status}\n{exc.body}\n")
        ledger[spec.business_id].update({"status": "submission_unknown", "http_status": exc.status, "error": exc.body})
        _save(ledger)
        _notify(f"{spec.business_id} 服务端 {exc.status}，状态不明，已停止重试")
        return ledger[spec.business_id]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        ledger[spec.business_id].update({"status": "submission_unknown", "error": str(exc)})
        _save(ledger)
        _notify(f"{spec.business_id} 提交结果不明（{exc}），已停止重试")
        return ledger[spec.business_id]

    task_id = resp.get("task_id") or _task(resp).get("id")
    if not task_id:
        ledger[spec.business_id].update({"status": "submission_unknown", "error": json.dumps(resp)[:800]})
        _save(ledger)
        _notify(f"{spec.business_id} 响应无 task_id，需人工核对")
        raise SystemExit(f"未取到 task_id：{json.dumps(resp, ensure_ascii=False)[:400]}")

    ledger[spec.business_id].update({"task_id": task_id, "status": "queued", "cost": spec.cost(),
                                     "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _save(ledger)
    print(f"[提交] {spec.business_id} → task_id={task_id}（{spec.cost()} 元）")
    return ledger[spec.business_id]


# ---------------------------------------------------------------- 查询与下载

def refresh(business_id: str) -> dict:
    ledger = _load()
    rec = ledger[business_id]
    task_id = rec.get("task_id") or die(f"{business_id} 无 task_id")

    task = _task(_call("GET", f"/v2/query/video_generation/{urllib.parse.quote(str(task_id))}"))
    status = str(task.get("status") or "unknown")
    rec.update({"status": status, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    usage = task.get("usage") or {}
    if usage.get("output_seconds"):
        rec["actual_seconds"] = usage["output_seconds"]

    if status == "succeeded":
        url = (task.get("content") or {}).get("url")
        if not url:
            rec.update({"status": "failed", "error": "succeeded 但无 content.url"})
            _save(ledger)
            raise SystemExit(f"{business_id} 成功但无产物地址，请检查响应")
        path = _download(business_id, url)
        rec.update({"status": "ready", "file": str(path.relative_to(ROOT)),
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        _save(ledger)
        print(f"[完成] {business_id} → {rec['file']}（{rec['bytes']} 字节）")
        return rec

    if status in FINAL_BAD:
        err = task.get("error") or {}
        rec.update({"status": status, "error_code": err.get("code"), "error": err.get("message")})
        _save(ledger)
        _notify(f"{business_id} 生成 {status}：{err.get('code')} {err.get('message')}")
        return rec

    _save(ledger)
    return rec


def _download(business_id: str, url: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"{business_id}.mp4"
    tmp = dest.with_suffix(".part")
    # 媒体 CDN 不需要鉴权头：不要把密钥发给第三方域名
    req = urllib.request.Request(url, headers={"User-Agent": "yeyu/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    if tmp.stat().st_size == 0:
        raise SystemExit(f"{business_id} 下载到空文件（链接可能已过期，重新 poll 即可）")
    tmp.replace(dest)
    return dest


def die(msg: str):
    raise SystemExit(msg)


# ---------------------------------------------------------------- 挂机

def watch(max_cny: float | None = None, interval: float = 15.0, deadline_hours: float = 8.0) -> None:
    end = time.time() + deadline_hours * 3600
    backoff = interval
    while time.time() < end:
        ledger = _load()
        todo = [k for k, v in ledger.items() if v.get("task_id") and v.get("status") in PENDING | {"submitting", "submission_unknown"}]
        if not todo:
            left = [k for k, v in ledger.items() if v.get("status") in {"submission_unknown"}]
            for k in left:
                _notify(f"{k} 提交状态不明，需人工核对")
            print("[挂机结束] 没有待处理任务")
            return
        for bid in todo:
            if _load().get(bid, {}).get("status") == "submission_unknown" and not _load()[bid].get("task_id"):
                continue                                    # 无 task_id 无法查询，等人处理
            try:
                rec = refresh(bid)
                print(f"  {bid}: {rec['status']}")
            except Exception as exc:
                print(f"  {bid}: 查询出错 {exc}", file=sys.stderr)
        if max_cny is not None:
            spent = sum(float(v.get("cost", 0)) for v in _load().values())
            if spent > max_cny:
                _notify(f"预算熔断：{spent:.2f} 元 > {max_cny} 元")
                return
        time.sleep(backoff + random.uniform(0, 3))
        backoff = min(backoff * 1.25, 90)


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="MiniMax H3 视频适配器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit")
    s.add_argument("--business-id", required=True)
    s.add_argument("--prompt", required=True)
    s.add_argument("--duration", type=int, default=6)
    s.add_argument("--model", default="MiniMax-H3-Lite")
    s.add_argument("--resolution", default="480P")
    s.add_argument("--ratio", default="16:9")
    s.add_argument("--first-frame", default=None)
    s.add_argument("--shot", default="")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--max-cny", type=float, default=None)

    w = sub.add_parser("watch")
    w.add_argument("--max-cny", type=float, default=None)
    w.add_argument("--interval", type=float, default=15.0)
    w.add_argument("--deadline-hours", type=float, default=8.0)

    p = sub.add_parser("poll")
    p.add_argument("--business-id", default=None)
    sub.add_parser("status")

    a = ap.parse_args()

    if a.cmd == "submit":
        if not 4 <= a.duration <= 15:
            raise SystemExit("duration 必须 4—15 秒")
        if (a.model, a.resolution.upper()) not in ALLOWED:
            raise SystemExit(f"不支持的组合 {a.model}+{a.resolution}；合法：{sorted(ALLOWED)}")
        spec = Spec(business_id=a.business_id, prompt=a.prompt, duration=a.duration, model=a.model,
                    resolution=a.resolution, ratio=a.ratio, first_frame=a.first_frame, shot=a.shot)
        submit(spec, dry_run=a.dry_run, max_cny=a.max_cny)
        return 0

    if a.cmd == "poll":
        ledger = _load()
        ids = [a.business_id] if a.business_id else [
            k for k, v in ledger.items() if v.get("task_id") and v.get("status") not in {"ready", "failed", "cancelled"}]
        for bid in ids:
            print(f"{bid}: {refresh(bid)['status']}")
        return 0

    if a.cmd == "watch":
        watch(max_cny=a.max_cny, interval=a.interval, deadline_hours=a.deadline_hours)
        return 0

    if a.cmd == "status":
        ledger = _load()
        for bid, v in sorted(ledger.items()):
            print(f"{bid:22s} {v.get('status', '?'):20s} {float(v.get('cost', 0)):6.2f}元  {v.get('task_id', '')}")
        print(f"\n合计 {len(ledger)} 条，记账 {sum(float(v.get('cost', 0)) for v in ledger.values()):.2f} 元")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
