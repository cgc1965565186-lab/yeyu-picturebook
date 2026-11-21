"""MiniMax H3 视频供应商：异步创建 → 轮询 → 下载，带断点恢复与重复计费防护。

契约（platform.minimax.cn 视频生成 V2）:
  POST /v2/video_generation                      → {"task_id": "..."}
  GET  /v2/query/video_generation/{task_id}      → {"task": {"status": "queued|running|succeeded|failed|cancelled",
                                                            "content": {"url": "..."}, "usage": {...}}}
  Authorization: Bearer <API_KEY>（账户管理 > 接口密钥）

与 AgnesVideoProvider 同一套边界约定：
  * 状态写盘 state_minimax.json，同一 run_id 可恢复；
  * 提交结果无法确认时进入 SUBMISSION_UNCERTAIN，**禁止自动重提**；
  * 下载失败只重下，不重建任务；
  * usage 记录输出秒数与金额，供预算对账。
"""
import base64
import hashlib
import json
import os
import time
import urllib.parse
from pathlib import Path

import httpx
from PIL import Image

from .providers import ProviderError, ProviderNotConfigured, VideoResult

BASE = os.getenv('MINIMAX_BASE_URL', 'https://api.minimax.cn').rstrip('/')
PRICE = {'480P': 0.16, '768P': 0.50, '2K': 0.80}
ALLOWED = {('MiniMax-H3', '768P'), ('MiniMax-H3', '2K'),
           ('MiniMax-H3-Lite', '480P'), ('MiniMax-H3-Lite', '768P'),
           ('MiniMax-H3-Highspeed', '480P'), ('MiniMax-H3-Highspeed', '768P'),
           ('MiniMax-H3-Max', '480P'), ('MiniMax-H3-Max', '768P')}


def _task(body):
    return body.get('task') if isinstance(body.get('task'), dict) else body


class MiniMaxVideoProvider:
    """shot: {prompt, seconds(4-15), reference_region?}；产物写到 output_dir/generated.mp4。"""

    def __init__(self, api_key, model='MiniMax-H3-Lite', resolution='480P', ratio='16:9',
                 timeout_seconds=2400, poll_seconds=8, transport=None, trust_env=False):
        if not api_key:
            raise ProviderNotConfigured('未配置 MINIMAX_API_KEY 或 YU_MINIMAX_KEY_FILE。')
        if (model, resolution) not in ALLOWED:
            raise ProviderNotConfigured(f'{model}+{resolution} 不是合法组合：{sorted(ALLOWED)}')
        self.api_key, self.model, self.resolution, self.ratio = api_key, model, resolution, ratio
        self.timeout_seconds, self.poll_seconds = timeout_seconds, poll_seconds
        self.transport, self.trust_env = transport, trust_env

    # ---- 请求体（V2 用 content 数组，不是旧的 prompt 字段）----
    def _body(self, shot, reference_path):
        seconds = int(shot.get('seconds', 6))
        if not 4 <= seconds <= 15:
            raise ProviderError('BAD_DURATION', f'MiniMax 单段需 4—15 秒，收到 {seconds}。')
        content = [{'type': 'text', 'text': str(shot.get('prompt', '')).strip()}]
        if not content[0]['text']:
            raise ProviderError('EMPTY_PROMPT', 'shot.prompt 为空。')
        ratio = self.ratio
        if reference_path and Path(reference_path).is_file():
            content.append({'type': 'image_url', 'image_url': {'url': self._data_uri(reference_path, shot.get('reference_region'))},
                            'role': 'first_frame'})
            ratio = 'adaptive'      # 图生视频：宽高比由输入图片决定
        return {'model': self.model, 'content': content, 'resolution': self.resolution.upper(),
                'duration': seconds, 'ratio': ratio, 'generate_audio': False}, ratio

    @staticmethod
    def _data_uri(path, region):
        with Image.open(path) as image:
            image = image.convert('RGB')
            if region:
                image = image.crop((round(region['x1'] * image.width), round(region['y1'] * image.height),
                                    round(region['x2'] * image.width), round(region['y2'] * image.height)))
            buffer = __import__('io').BytesIO()
            image.save(buffer, format='PNG')
            return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()

    def _error(self, response, body):
        message = response.text[:1200]
        for secret in (self.api_key,):
            message = message.replace(secret, '[redacted]')
        return dict(http_status=response.status_code, message=message,
                    body_sha256=hashlib.sha256(response.content).hexdigest())

    # ---- 主流程 ----
    def generate(self, shot, reference_path, output_dir, run_id):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        state_path = output_dir / 'state_minimax.json'
        state = json.loads(state_path.read_text()) if state_path.is_file() else {}
        body, _ratio = self._body(shot, reference_path)
        video = output_dir / 'generated.mp4'

        def save():
            temp = state_path.with_suffix('.tmp')
            temp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            temp.replace(state_path)

        if state.get('status') == 'downloaded' and video.is_file():
            return VideoResult(video, state['task_id'], self.model, state.get('usage', {}))

        headers = {'Authorization': 'Bearer ' + self.api_key, 'Content-Type': 'application/json'}
        with httpx.Client(timeout=90, transport=self.transport, trust_env=self.trust_env) as client:
            if not state.get('task_id'):
                if state.get('submission_started'):
                    raise ProviderError('SUBMISSION_UNCERTAIN', '提交可能已被受理；先核查平台任务，禁止自动重投。')
                state.update(submission_started=True, status='submitting', submitted_at=time.time())
                save()
                try:
                    response = client.post(f'{BASE}/v2/video_generation', headers=headers, json=body)
                except httpx.HTTPError:
                    state['status'] = 'submission_uncertain'
                    save()
                    raise ProviderError('SUBMISSION_UNCERTAIN', '未确认提交结果，请先核查平台任务。')
                payload = response.json() if response.content else {}
                if os.getenv('MINIMAX_DUMP'):
                    (output_dir / 'minimax_submit_raw.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2))
                task_id = payload.get('task_id') if isinstance(payload, dict) else None
                if not task_id:
                    state['last_error'] = self._error(response, body)
                    state['status'] = 'submission_uncertain' if response.status_code >= 500 else 'rejected'
                    save()
                    raise ProviderError('SUBMISSION_UNCERTAIN', '响应无 task_id；已保留证据，禁止自动重投。')
                state.update(task_id=task_id, status='queued', submit_http_status=response.status_code)
                save()

            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                try:
                    response = client.get(f'{BASE}/v2/query/video_generation/{urllib.parse.quote(str(state["task_id"]))}',
                                          headers=headers)
                    response.raise_for_status()
                    task = _task(response.json())
                except (httpx.HTTPError, ValueError):
                    state['poll_errors'] = state.get('poll_errors', 0) + 1
                    save()
                    time.sleep(self.poll_seconds)
                    continue
                status = str(task.get('status') or 'unknown')
                state.update(status=status, polls=state.get('polls', 0) + 1, last_poll_at=time.time())
                save()
                if status in ('failed', 'cancelled'):
                    err = task.get('error') or {}
                    raise ProviderError('MINIMAX_GENERATION_FAILED',
                                        f'供应商 {status}：{err.get("code")} {err.get("message")}', False)
                if status == 'succeeded':
                    url = (task.get('content') or {}).get('url')
                    if not isinstance(url, str) or not url.startswith('https://'):
                        raise ProviderError('INVALID_VIDEO_URL', '成功但未返回有效 HTTPS 媒体地址。', True)
                    self._download(url, video)
                    usage = task.get('usage') or {}
                    out_seconds = int(usage.get('output_seconds') or int(body['duration']))
                    usage = dict(usage, external_calls=1, accepted_tasks=1, output_seconds=out_seconds,
                                 cost=round(out_seconds * PRICE.get(self.resolution, 0), 2))
                    state.update(status='downloaded', output_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
                                 completed_at=time.time(), usage=usage)
                    save()
                    return VideoResult(video, state['task_id'], self.model, usage)
                time.sleep(self.poll_seconds)
        raise ProviderError('MINIMAX_POLL_TIMEOUT', '等待远端结果超时；可用同一 run_id 恢复查询。', True)

    def _download(self, url, video):
        # 媒体 CDN 不带鉴权头，避免把密钥发给第三方域名
        try:
            with httpx.Client(timeout=300, transport=self.transport, follow_redirects=True, trust_env=self.trust_env) as media_client:
                with media_client.stream('GET', url) as media:
                    media.raise_for_status()
                    partial = video.with_suffix('.part')
                    with partial.open('wb') as file:
                        for chunk in media.iter_bytes():
                            file.write(chunk)
                    partial.replace(video)
        except httpx.HTTPError:
            raise ProviderError('VIDEO_DOWNLOAD_FAILED', '视频已完成，下载失败；重试只恢复查询和下载。', True)


def configured_minimax_provider():
    key = os.getenv('MINIMAX_API_KEY', '')
    if not key and os.getenv('YU_MINIMAX_KEY_FILE'):
        key = Path(os.environ['YU_MINIMAX_KEY_FILE']).read_text().strip()
    return MiniMaxVideoProvider(key, model=os.getenv('YU_MINIMAX_MODEL', 'MiniMax-H3-Lite'),
                                resolution=os.getenv('YU_MINIMAX_RESOLUTION', '480P'),
                                ratio=os.getenv('YU_MINIMAX_RATIO', '16:9'))
