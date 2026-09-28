"""Public-source discovery and bounded extraction, with local provenance."""
from datetime import datetime, timezone
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import socket
import threading
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

MAX_BYTES = 4 * 1024 * 1024
MAX_CHARS = 60000


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def resolve_public_url(url):
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("资料链接须为不带账号密码的公开 HTTP(S) 地址。")
    if parts.port not in {None, 80, 443}:
        raise ValueError("只支持公开网页的 80 / 443 端口。")
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("资料采集只允许公网地址，不能抓取本机或内网服务。")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, "")), addresses[0][4][0]


def public_url(url):
    return resolve_public_url(url)[0]


def search_web(query, limit=6):
    if not 2 <= len(query.strip()) <= 300:
        raise ValueError("请输入 2–300 个字符的研究主题。")
    from ddgs import DDGS
    try:
        raw = DDGS(timeout=15).text(query, max_results=min(10, limit), region="wt-wt")
    except Exception as exc:
        raise ValueError("公开搜索暂时不可用或被限流，请稍后重试，也可以直接添加链接或上传资料。") from exc
    results, seen = [], set()
    for item in raw:
        url = item.get("href", "")
        if url in seen or urlsplit(url).scheme not in {"http", "https"}:
            continue
        seen.add(url)
        results.append({"title": item.get("title", url), "url": url, "snippet": item.get("body", ""), "discovered_at": utc_now()})
    return results


def parse_document(name, body, content_type=""):
    if len(body) > MAX_BYTES:
        raise ValueError("文件超过 4 MB，请拆分后再导入。")
    lower = name.lower().split("?")[0]
    if body.startswith(b"%PDF") or lower.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted or len(reader.pages) > 60:
            raise ValueError("PDF 须未加密且不超过 60 页。")
        pages = [page.extract_text() or "" for page in reader.pages]
        if len("".join(pages).strip()) < 40:
            raise ValueError("PDF 没有可提取正文，扫描文档需先 OCR。")
        text = "\n\n".join(f"[第 {i} 页]\n{text}" for i, text in enumerate(pages, 1))
        kind = "pdf"
    elif "html" in content_type or lower.endswith((".html", ".htm")) or b"<html" in body[:1000].lower() or b"<!doctype" in body[:1000].lower():
        import trafilatura
        text = trafilatura.extract(body, include_comments=False, include_tables=True, favor_precision=True) or ""
        kind = "web"
    else:
        text = body.decode("utf-8-sig", errors="strict")
        kind = "text"
    text = text.strip()
    if len(text) < 40:
        raise ValueError("未提取到足够正文。可能是扫描 PDF、动态网页或受限内容；请改用文本文件。")
    truncated = len(text) > MAX_CHARS
    return {"text": text[:MAX_CHARS], "kind": kind, "truncated": truncated, "original_chars": len(text)}


def fetch_source(url, title=""):
    current = url
    with httpx.Client(timeout=httpx.Timeout(25, connect=10), follow_redirects=False, trust_env=False, headers={"User-Agent": "RetrievalLab/0.2 (local research tool)", "Accept": "text/html,text/plain,application/pdf"}) as client:
        for _ in range(6):
            current, address = resolve_public_url(current)
            parts = urlsplit(current)
            # Connect to the IP we validated, while preserving Host and TLS SNI.
            # This prevents a second DNS lookup from rebinding to an internal IP.
            target = httpx.URL(current).copy_with(host=address)
            with client.stream("GET", target, headers={"Host": parts.netloc}, extensions={"sni_hostname": parts.hostname}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if not response.headers.get("location"):
                        raise ValueError("网页重定向缺少目标地址。")
                    current = urljoin(current, response.headers["location"])
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if not any(t in content_type for t in ("text/", "html", "pdf", "octet-stream")):
                    raise ValueError("只支持文本网页、TXT、Markdown 和文本型 PDF。")
                body = bytearray()
                for part in response.iter_bytes():
                    body.extend(part)
                    if len(body) > MAX_BYTES:
                        raise ValueError("网页正文下载超过 4 MB 上限。")
                result = parse_document(current, bytes(body), content_type)
                result.update({"title": title or urlsplit(current).hostname, "url": current, "requested_url": url, "collected_at": utc_now()})
                return result
    raise ValueError("网页重定向次数过多。")


class SourceStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def add(self, source):
        text = source["text"].strip()
        identifier = hashlib.sha256(text.encode()).hexdigest()[:24]
        with self.lock:
            path = self.directory / f"{identifier}.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            item = {**source, "id": identifier, "chars": len(text), "status": "collected", "collected_at": source.get("collected_at", utc_now()),
                    "doc_id": "doc-" + hashlib.md5(text.encode()).hexdigest(), "file_path": f"source-{identifier}.txt"}
            self._write(path, item)
            return item

    def _write(self, path, value):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def list(self):
        with self.lock:
            values = [json.loads(p.read_text(encoding="utf-8")) for p in self.directory.glob("*.json")]
        return sorted(values, key=lambda item: item["collected_at"], reverse=True)

    def get(self, identifier):
        if not identifier or any(c not in "0123456789abcdef" for c in identifier):
            raise ValueError("无效的资料 ID。")
        with self.lock:
            return json.loads((self.directory / f"{identifier}.json").read_text(encoding="utf-8"))

    def update(self, identifier, **changes):
        with self.lock:
            item = self.get(identifier)
            item.update(changes)
            self._write(self.directory / f"{identifier}.json", item)
            return item
