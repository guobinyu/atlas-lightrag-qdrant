"""Independent Seedream / OpenAI Images connections. Credentials never enter task snapshots."""
import base64
from dataclasses import dataclass, asdict, field
from io import BytesIO
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image

SIZES = ('1024x1024', '1536x1024', '1024x1536')
SEEDREAM_SIZES = ('2048x2048', '2496x1664', '1664x2496')
PROVIDERS = {'seedream': ('火山引擎豆包 Seedream', 'https://ark.cn-beijing.volces.com/api/v3', 'doubao-seedream-5-0-lite-260128'),
             'openai': ('OpenAI Images（兼容历史配置）', 'https://api.openai.com/v1', 'gpt-image-2.5-sunburst')}


def image_sizes(provider):
    return SEEDREAM_SIZES if provider == 'seedream' else SIZES


def compatible_size(size, provider):
    target = image_sizes(provider)
    if size in target:
        return size
    origin = SIZES if provider == 'seedream' else SEEDREAM_SIZES
    if size in origin:
        return target[origin.index(size)]
    raise ValueError('不支持的图稿像素规格。')


MAX_IMAGE_BYTES = 32 * 1024 * 1024


@dataclass
class ImageSettings:
    endpoint: str = 'https://ark.cn-beijing.volces.com/api/v3'
    model: str = 'doubao-seedream-5-0-lite-260128'
    api_key: str = field(default='', repr=False)
    timeout: int = 300
    provider: str = 'seedream'

    @classmethod
    def from_saved(cls, value):
        # Before providers were introduced, every saved configuration used OpenAI.
        return cls(**{**value, 'provider': value.get('provider', 'openai')})

    def validate(self):
        if self.provider not in PROVIDERS:
            raise ValueError('请选择支持的图片服务。')
        p = urlsplit(self.endpoint)
        if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError('图片接口需为不含凭证或查询参数的 HTTP(S) API 基础地址。')
        if p.path.rstrip('/').endswith(('/generations', '/edits', '/chat/completions')):
            raise ValueError('请填写 API 基础地址（例如 https://ark.cn-beijing.volces.com/api/v3），不包含生成或编辑路径。')
        if not self.model.strip() or len(self.model) > 200 or not 15 <= self.timeout <= 600:
            raise ValueError('请填写图片模型名称，超时需为 15–600 秒。')
        return self

    def public(self):
        return {k: v for k, v in asdict(self).items() if k != 'api_key'}


def png_info(raw):
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('图片为空或超过 32 MB。')
    try:
        with Image.open(BytesIO(raw)) as im:
            if im.format != 'PNG' or im.width * im.height > 20_000_000:
                raise ValueError('需要有效的 PNG 图片，且不超过 2000 万像素。')
            info = {'width': im.width, 'height': im.height, 'mime': 'image/png'}
            im.verify()
        with Image.open(BytesIO(raw)) as im:
            im.load()
        return info
    except Exception as exc:
        raise ValueError('服务没有返回可读取的 PNG 图片。') from exc


def write_png(path, raw):
    info = png_info(raw)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_bytes(raw)
    temp.replace(path)
    return info


class ImageService:
    def __init__(self, path):
        self.path = path
        self.settings = ImageSettings.from_saved(json.loads(path.read_text())) if path.exists() else ImageSettings()

    @property
    def configured(self):
        return bool(self.settings.api_key.strip() and self.settings.model.strip())

    def save(self, settings):
        settings.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(asdict(settings), f, ensure_ascii=False, indent=2)
        temp.replace(self.path)
        self.settings = settings

    async def render(self, prompt, size, source=None, config=None):
        # Freeze routing/parameters, but resolve credentials only at execution time.
        cfg = ImageSettings.from_saved({**(config or self.settings.public()), 'api_key': self.settings.api_key}).validate()
        if cfg.endpoint.rstrip('/') != self.settings.endpoint.rstrip('/') or cfg.provider != self.settings.provider:
            raise ValueError('图片接口地址已变化或服务商已切换，请恢复原地址重试或重新生成。')
        if not self.configured:
            raise ValueError('请先配置独立的图片 API Key。')
        if size not in image_sizes(cfg.provider):
            raise ValueError('不支持的图稿像素规格。')
        if source is not None:
            png_info(source)
            if cfg.provider == 'seedream' and len(source) > 30 * 1024 * 1024:
                raise ValueError('Seedream 原图不能超过 30 MB。')
        payload = {'model': cfg.model, 'prompt': prompt, 'n': 1, 'size': size, 'quality': 'medium', 'output_format': 'png'}
        if cfg.provider == 'seedream':
            payload = {'model': cfg.model, 'prompt': prompt, 'size': size, 'output_format': 'png',
                       'response_format': 'b64_json', 'watermark': True}
            if source is not None:
                payload['image'] = 'data:image/png;base64,' + base64.b64encode(source).decode('ascii')
        headers = {'Authorization': 'Bearer ' + cfg.api_key}
        try:
            async with httpx.AsyncClient(timeout=cfg.timeout, follow_redirects=False) as client:
                if source is None or cfg.provider == 'seedream':
                    response = await client.post(cfg.endpoint.rstrip('/') + '/images/generations', headers=headers, json=payload)
                else:
                    response = await client.post(cfg.endpoint.rstrip('/') + '/images/edits', headers=headers,
                                                 data={k: str(v) for k, v in payload.items()},
                                                 files={'image': ('source.png', source, 'image/png')})
                response.raise_for_status()
                result = response.json()
            encoded = result['data'][0]['b64_json']
            if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
                raise ValueError('图片数据无效或过大。')
            raw = base64.b64decode(encoded, validate=True)
            png_info(raw)
            return raw
        except httpx.HTTPStatusError as exc:
            raise ValueError(image_error(exc.response, cfg.api_key)) from None
        except httpx.RequestError:
            raise ValueError('图片服务连接失败或超时，可重试；超时请求可能已在服务端产生费用。') from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise ValueError('图片服务未返回有效的 base64 PNG 图片。') from None


def image_error(response, api_key):
    """Retain provider diagnostics without storing credentials or an entire response."""
    def clean(value):
        if not isinstance(value, str):
            return ''
        if api_key:
            value = value.replace(api_key, '[redacted]')
        value = re.sub(r'(?i)Bearer\s+[^\s,;]+', 'Bearer [redacted]', value)
        value = re.sub(r'(?i)(api[_ -]?key|authorization)\s*[:=]\s*[^\s,;]+', r'\1=[redacted]', value)
        return ' '.join(value.split())[:1200]
    try:
        body = response.json()
        error = body.get('error', {}) if isinstance(body, dict) else {}
        error = error if isinstance(error, dict) else {}
    except ValueError:
        body, error = {}, {}
    code = clean(error.get('code'))
    message = clean(error.get('message'))
    request_id = clean(response.headers.get('x-request-id') or response.headers.get('x-tt-logid')
                       or (body.get('request_id') if isinstance(body, dict) else None))
    text = f'图片服务返回 HTTP {response.status_code}'
    if code:
        text += ' · ' + code
    if message:
        text += '：' + message
    if request_id:
        text += '（请求 ID：' + request_id + '）'
    if response.status_code == 429:
        text += '。429 可能是额度或速率限制，也可能是服务资源紧张；请按上述具体错误详情排查，勿连续提交。'
    elif not message:
        text += '，请检查权限、额度、模型及接口设置。'
    return text
