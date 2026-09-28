"""Runtime-configurable extraction LLM. Never exposes hidden model reasoning."""
from dataclasses import dataclass, asdict, field
import json
import os
import re
import time
from urllib.parse import urlsplit

import httpx


@dataclass
class LLMSettings:
    provider: str = "http"
    endpoint: str = "http://127.0.0.1:11434/v1/chat/completions"
    model: str = ""
    api_key: str = field(default="", repr=False)
    timeout: int = 180

    def validate(self):
        parts = urlsplit(self.endpoint)
        if self.provider not in {"ollama", "http"} or parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("请选择 Ollama 或兼容 Chat Completions 的 HTTP 服务，并填写完整接口地址。")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("接口地址不能包含凭证或查询参数，请使用独立 API Key 字段。")
        if not self.model.strip() or len(self.model) > 200:
            raise ValueError("请填写有效的模型名。")
        if not 15 <= self.timeout <= 600:
            raise ValueError("模型超时须在 15–600 秒之间。")
        return self

    def public(self):
        return {"provider": self.provider, "endpoint": self.endpoint, "model": self.model, "timeout": self.timeout, "has_key": bool(self.api_key)}


class ExtractionLLM:
    def __init__(self, path, emit):
        self.path = path
        self.emit = emit
        self.settings = LLMSettings()
        if path.exists():
            self.settings = LLMSettings(**json.loads(path.read_text(encoding="utf-8")))
        elif os.getenv("LLM_MODEL"):
            self.settings = LLMSettings(provider=os.getenv("LLM_PROVIDER", "http"), endpoint=os.getenv("LLM_URL", "http://127.0.0.1:11434/v1/chat/completions"), model=os.environ["LLM_MODEL"], api_key=os.getenv("LLM_API_KEY", ""))

    @property
    def configured(self):
        return bool(self.settings.model.strip())

    def save(self, settings):
        settings.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Private file from creation, not chmod after writing a readable secret.
        temp = self.path.with_suffix(".tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(asdict(settings), file, ensure_ascii=False, indent=2)
        temp.replace(self.path)
        self.settings = settings

    async def check(self, settings):
        settings.validate()
        content, usage = await self._request(settings, [{"role": "user", "content": "只回复 OK"}])
        if not content.strip():
            raise ValueError("模型没有返回可用的文本内容。")
        return {"model": settings.model, "reply": content[:100], "usage": usage}

    async def __call__(self, prompt, system_prompt=None, history_messages=None, **kwargs):
        if not self.configured:
            raise ValueError("请先在模型设置页配置用于 LightRAG 实体关系抽取的 LLM。")
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(history_messages or [])
        messages.append({"role": "user", "content": prompt})
        call_id = str(time.time_ns())
        started = time.perf_counter()
        self.emit("llm.started", {"call_id": call_id, "model": self.settings.model, "input_chars": sum(len(m["content"]) for m in messages)})
        try:
            content, usage = await self._request(self.settings, messages)
        except Exception as exc:
            self.emit("llm.failed", {"call_id": call_id, "error_type": type(exc).__name__})
            raise
        self.emit("llm.completed", {"call_id": call_id, "model": self.settings.model, "output_chars": len(content), "usage": usage, "duration_ms": round((time.perf_counter() - started) * 1000, 1)})
        return content

    async def _request(self, cfg, messages):
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        if cfg.provider == "ollama":
            payload = {"model": cfg.model, "messages": messages, "stream": False, "think": False,
                       "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 4096}}
        else:
            payload = {"model": cfg.model, "messages": messages, "stream": False, "temperature": 0}
        try:
            async with httpx.AsyncClient(timeout=cfg.timeout) as client:
                response = await client.post(cfg.endpoint, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"模型服务返回 HTTP {exc.response.status_code}，请检查模型名称、接口路径和认证。") from None
        except httpx.RequestError:
            raise ValueError("模型连接失败或超时，请检查服务是否启动与模型是否已下载。") from None
        if cfg.provider == "ollama":
            content = data.get("message", {}).get("content", "")
            usage = {"input_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")}
        else:
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
        if not isinstance(content, str):
            raise ValueError("模型响应缺少文本 content。")
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if not content:
            raise ValueError("模型返回了空文本，无法抽取实体关系。")
        return content, usage
