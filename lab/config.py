from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "data"
    workspace: str = "silk_culture_demo"
    qdrant_mode: str = "local"
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = field(default="", repr=False)
    embedding_provider: str = "fastembed"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    embedding_url: str = "http://127.0.0.1:11434/v1/embeddings"
    embedding_api_key: str = field(default="", repr=False)
    query_prefix: str = ""
    document_prefix: str = ""
    seed_demo: bool = True
    sample_limit: int = 2000
    timeout: float = 60.0
    working_dir: Path | None = None

    @property
    def rag_dir(self) -> Path:
        return self.working_dir or self.data_dir / "lightrag"

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "runs" / self.workspace

    def validate(self):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.workspace):
            raise ValueError("WORKSPACE 只能含字母、数字、下划线和短横线，最长 64 字符。")
        if self.qdrant_mode not in {"local", "server"}:
            raise ValueError("QDRANT_MODE 必须为 local 或 server。")
        if self.embedding_provider not in {"fastembed", "http"}:
            raise ValueError("EMBEDDING_PROVIDER 必须为 fastembed 或 http。")
        if not 1 <= self.embedding_dim <= 65536 or not 1 <= self.sample_limit <= 2000:
            raise ValueError("向量维度或背景样本数量不合法。")
        from urllib.parse import urlsplit
        for url in (self.qdrant_url, self.embedding_url):
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("服务地址必须是有效的 HTTP(S) URL。")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("请通过独立的 API Key 配置认证，服务地址不要带凭证或查询参数。")
        override = os.getenv("QDRANT_WORKSPACE", "").strip()
        if override and override != self.workspace:
            raise ValueError("QDRANT_WORKSPACE 与 WORKSPACE 不一致，请消除工作区覆盖后再启动。")
        return self

    def embedding_identity(self):
        return {
            "provider": self.embedding_provider,
            "model": self.embedding_model,
            "dimension": self.embedding_dim,
            "endpoint": self.embedding_url if self.embedding_provider == "http" else "local-onnx",
            "query_prefix": self.query_prefix,
            "document_prefix": self.document_prefix,
        }


def load_settings() -> Settings:
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    def get(name, default):
        return values.get(name, default)
    data_dir = Path(get("LAB_DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
    mode = get("QDRANT_MODE", "local")
    return Settings(
        data_dir=data_dir,
        workspace=get("WORKSPACE", "silk_culture_demo"),
        qdrant_mode=mode,
        qdrant_url=get("QDRANT_URL", "http://127.0.0.1:6333"),
        qdrant_api_key=get("QDRANT_API_KEY", ""),
        embedding_provider=get("EMBEDDING_PROVIDER", "fastembed"),
        embedding_model=get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"),
        embedding_dim=int(get("EMBEDDING_DIM", "512")),
        embedding_url=get("EMBEDDING_URL", "http://127.0.0.1:11434/v1/embeddings"),
        embedding_api_key=get("EMBEDDING_API_KEY", ""),
        query_prefix=get("QUERY_PREFIX", ""),
        document_prefix=get("DOCUMENT_PREFIX", ""),
        seed_demo=get("SEED_DEMO", "true" if mode == "local" else "false").lower() == "true",
        sample_limit=int(get("SAMPLE_LIMIT", "2000")),
        timeout=float(get("REQUEST_TIMEOUT", "60")),
        working_dir=Path(values["LIGHTRAG_WORKING_DIR"]).expanduser().resolve() if values.get("LIGHTRAG_WORKING_DIR") else None,
    ).validate()
