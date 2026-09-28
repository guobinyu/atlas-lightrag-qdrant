import socket

import pytest

from lab.sources import SourceStore, parse_document, public_url, resolve_public_url


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost/", "http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/", "https://user:secret@example.com/", "http://example.com:6333/"])
def test_private_and_credential_urls_rejected(url):
    with pytest.raises(ValueError):
        public_url(url)


def test_dns_mixed_public_and_private_is_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("10.0.0.1", 443))])
    with pytest.raises(ValueError, match="公网"):
        public_url("https://example.com")


def test_resolve_pins_the_validated_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    assert resolve_public_url("https://example.com/#section") == ("https://example.com/", "8.8.8.8")


def test_sources_deduplicate_content_and_keep_state(tmp_path):
    store = SourceStore(tmp_path)
    first = store.add({"text": "同一份资料" * 20, "title": "first", "url": ""})
    store.update(first["id"], status="processed")
    duplicate = store.add({"text": "同一份资料" * 20, "title": "second", "url": ""})
    assert len(store.list()) == 1
    assert duplicate["status"] == "processed"
    assert duplicate["doc_id"] == first["doc_id"]
    assert duplicate["title"] == "first"
    with pytest.raises(ValueError):
        store.get("../file")


def test_extraction_truncation_and_empty_input():
    with pytest.raises(ValueError):
        parse_document("empty.txt", b"tiny")
    data = parse_document("long.md", ("知识" * 31000).encode())
    assert data["truncated"]
    assert len(data["text"]) == 60000
    html = b'<html><article><h1>Research</h1><p>' + b'Public research content with useful facts. ' * 30 + b'</p></article></html>'
    text = parse_document("document.html", html)["text"]
    assert "<article>" not in text
    assert "Public research content" in text
