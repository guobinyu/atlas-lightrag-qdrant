import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from lab.llm import ExtractionLLM, LLMSettings


def test_http_model_protocol_redaction_and_thinking_exclusion(tmp_path):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            seen.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            if self.path == "/unauthorized":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"secret=TEST_KEY")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"content": "<think>PRIVATE_REASONING</think>OK", "reasoning_content": "PRIVATE_REASONING"}}], "usage": {"total_tokens": 8}}).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    events = []
    llm = ExtractionLLM(tmp_path / "llm.json", lambda kind, data: events.append((kind, data)))
    llm.settings = LLMSettings("http", f"http://127.0.0.1:{server.server_port}/v1/chat/completions", "model-for-test", "TEST_KEY", 30)
    try:
        result = asyncio.run(llm("query", system_prompt="system", history_messages=[{"role": "assistant", "content": "earlier"}], hashing_kv=object()))
        assert result == "OK"
        assert seen[0][0] == "/v1/chat/completions"
        assert seen[0][1] == "Bearer TEST_KEY"
        assert [m["role"] for m in seen[0][2]["messages"]] == ["system", "assistant", "user"]
        assert seen[0][2]["model"] == "model-for-test"
        assert "TEST_KEY" not in json.dumps(events)
        assert "PRIVATE_REASONING" not in json.dumps(events)
        llm.settings.endpoint = f"http://127.0.0.1:{server.server_port}/unauthorized"
        with pytest.raises(ValueError, match="HTTP 401") as exc:
            asyncio.run(llm("query"))
        assert "TEST_KEY" not in str(exc.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
