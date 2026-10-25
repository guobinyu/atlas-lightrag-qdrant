"""Domestic web search through the Ark Responses API web_search tool.

The model runs one search and the cited pages (url_citation annotations) become the
result list; the model's own summary text is not used as evidence. Pages are still read
and date-checked by the existing collection code.
"""
from dataclasses import dataclass, asdict, field
import json
import os
import re
from urllib.parse import urlsplit

import httpx

from lab.sources import utc_now

PROVIDERS = {'ark': '火山方舟联网搜索', 'ddgs': '免 Key 公开搜索（DDGS，境外引擎）'}
# Example model from the Ark web_search tool docs; any Ark model with tool calling works.
DEFAULT_MODEL = 'doubao-seed-2-1-pro-260915'


@dataclass
class SearchSettings:
    provider: str = 'ark'
    endpoint: str = 'https://ark.cn-beijing.volces.com/api/v3'
    model: str = DEFAULT_MODEL
    api_key: str = field(default='', repr=False)
    use_image_key: bool = True
    timeout: int = 120
    limit: int = 10

    def validate(self):
        if self.provider not in PROVIDERS:
            raise ValueError('请选择支持的搜索服务。')
        if self.provider == 'ddgs':
            return self
        p = urlsplit(self.endpoint)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError('搜索接口需为不含凭证或查询参数的 HTTPS 基础地址。')
        if p.path.rstrip('/').endswith(('/responses', '/chat/completions')):
            raise ValueError('请填写 API 基础地址（例如 https://ark.cn-beijing.volces.com/api/v3），不包含 /responses。')
        if not self.model.strip() or len(self.model) > 200:
            raise ValueError('请填写支持工具调用的方舟模型 ID 或推理接入点 ID。')
        if not 15 <= self.timeout <= 600 or not 1 <= self.limit <= 20:
            raise ValueError('超时需为 15–600 秒，每次结果数为 1–20 条。')
        return self

    def public(self):
        return {k: v for k, v in asdict(self).items() if k != 'api_key'} | {'has_key': bool(self.api_key)}


def search_error(response, key):
    """Provider diagnostics without credentials or the whole body."""
    def clean(value):
        if not isinstance(value, str):
            return ''
        if key:
            value = value.replace(key, '[redacted]')
        value = re.sub(r'(?i)Bearer\s+[^\s,;]+', 'Bearer [redacted]', value)
        return ' '.join(value.split())[:600]
    try:
        body = response.json()
        error = body.get('error', {}) if isinstance(body, dict) else {}
        error = error if isinstance(error, dict) else {}
    except ValueError:
        error = {}
    text = f'搜索服务返回 HTTP {response.status_code}'
    code, message = clean(error.get('code')), clean(error.get('message'))
    if code:
        text += ' · ' + code
    if message:
        text += '：' + message
    if code == 'ToolNotOpen':
        text += '。模型和 Key 可用，但账号尚未开通联网内容插件：请在火山引擎控制台开通（按调用次数计费）后重试。'
    elif response.status_code == 404:
        text += '。请确认模型 ID 正确、已在方舟控制台开通，且支持联网搜索工具。'
    return text


def citations(data, limit):
    """url_citation annotations in output order, de-duplicated by URL."""
    results, seen = [], set()
    for item in data.get('output') or []:
        if not isinstance(item, dict) or item.get('type') != 'message':
            continue
        for part in item.get('content') or []:
            for note in (part.get('annotations') or []) if isinstance(part, dict) else []:
                url = note.get('url') if isinstance(note, dict) and note.get('type') == 'url_citation' else None
                if not isinstance(url, str) or urlsplit(url).scheme not in ('http', 'https') or url in seen:
                    continue
                seen.add(url)
                results.append({'title': str(note.get('title') or url)[:300], 'url': url,
                                'snippet': str(note.get('summary') or '')[:1000], 'site_name': str(note.get('site_name') or ''),
                                'published': str(note.get('publish_time') or ''), 'discovered_at': utc_now()})
    return results[:limit]


class SearchService:
    def __init__(self, path, image_key=lambda: '', transport=None):
        self.path = path
        self.image_key = image_key
        self.transport = transport
        self.settings = SearchSettings(**json.loads(path.read_text(encoding='utf-8'))) if path.exists() else SearchSettings()

    def key(self, settings=None):
        cfg = settings or self.settings
        return cfg.api_key or (self.image_key() if cfg.use_image_key else '')

    @property
    def active(self):
        cfg = self.settings
        return cfg.provider == 'ark' and bool(cfg.model.strip()) and bool(self.key())

    def save(self, settings):
        settings.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(asdict(settings), f, ensure_ascii=False, indent=2)
        temp.replace(self.path)
        self.settings = settings

    def search(self, query, limit=10):
        cfg = self.settings.validate()
        key = self.key()
        if not key:
            raise ValueError('未配置方舟 API Key：请在搜索服务中填写，或在图片模型中保存方舟 Key 后选择复用。')
        payload = {'model': cfg.model, 'stream': False, 'max_tool_calls': 1,
                   'tools': [{'type': 'web_search', 'limit': min(limit, cfg.limit), 'max_keyword': 1}],
                   'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text':
                              '请使用联网搜索查找以下检索词的中文网页，只搜索一次，并用一句话概括结果：' + query}]}]}
        try:
            with httpx.Client(timeout=cfg.timeout, follow_redirects=False, transport=self.transport) as client:
                response = client.post(cfg.endpoint.rstrip('/') + '/responses', json=payload,
                                       headers={'Authorization': 'Bearer ' + key})
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            raise ValueError(search_error(exc.response, key)) from None
        except httpx.RequestError as exc:
            raise ValueError(f'搜索服务连接失败或超时（{type(exc).__name__}），请检查网络与接口地址。') from None
        except ValueError:
            raise ValueError('搜索服务返回的不是 JSON。') from None
        return citations(data, limit)
