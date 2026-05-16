import importlib
import io
import json
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError


class FakeResponse:
    def __init__(self, body=b"{}", status=200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def reload_nia(monkeypatch, tmp_path, env_key: str | None = "test-key"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("NIA_API_KEY", raising=False)
    if env_key is not None:
        monkeypatch.setenv("NIA_API_KEY", env_key)
    import tools.nia_tool as nia_tool
    return importlib.reload(nia_tool)


def capture_request(monkeypatch, body=b'{"ok":true}'):
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["data"] = req.data
        captured["timeout"] = timeout
        return FakeResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


def decode_result(result):
    return json.loads(result)


def test_api_key_prefers_env_without_leaking(monkeypatch, tmp_path):
    nia = reload_nia(monkeypatch, tmp_path, env_key="env-secret")
    key_file = tmp_path / ".config" / "nia" / "api_key"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("file-secret")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert nia._get_api_key() == "env-secret"
    assert nia._check_nia_available() is True


def test_api_key_falls_back_to_config_file(monkeypatch, tmp_path):
    nia = reload_nia(monkeypatch, tmp_path, env_key=None)
    key_file = tmp_path / ".config" / "nia" / "api_key"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("file-secret\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert nia._get_api_key() == "file-secret"
    assert nia._check_nia_available() is True


def test_nia_usage_sends_auth_header_and_redacts_errors(monkeypatch, tmp_path):
    nia = reload_nia(monkeypatch, tmp_path, env_key="secret-token")
    captured = capture_request(monkeypatch, b'{"tier":"pro"}')

    result = decode_result(nia.nia_usage_tool({}))

    assert result == {"tier": "pro"}
    assert captured["method"] == "GET"
    assert captured["url"] == "https://apigcp.trynia.ai/v2/usage"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in json.dumps(result)


def test_repo_index_defaults_private(monkeypatch, tmp_path):
    nia = reload_nia(monkeypatch, tmp_path)
    captured = capture_request(monkeypatch, b'{"id":"repo-id"}')

    result = decode_result(nia.nia_repos_tool({"action": "index", "repository": "NousResearch/hermes-agent", "ref": "main"}))
    body = json.loads(captured["data"].decode())

    assert result["id"] == "repo-id"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://apigcp.trynia.ai/v2/sources"
    assert body == {"type": "repository", "repository": "NousResearch/hermes-agent", "ref": "main"}
    assert "add_as_global_source" not in body


def test_repo_grep_posts_expected_body(monkeypatch, tmp_path):
    nia = reload_nia(monkeypatch, tmp_path)
    captured = capture_request(monkeypatch, b'{"matches":[]}')

    nia.nia_repos_tool({
        "action": "grep",
        "repository": "owner/repo",
        "pattern": "registry.register",
        "path": "tools",
        "max_total": 25,
        "fixed_string": True,
    })
    body = json.loads(captured["data"].decode())

    assert captured["method"] == "POST"
    assert captured["url"] == "https://apigcp.trynia.ai/v2/sources/owner%2Frepo/grep"
    assert body["pattern"] == "registry.register"
    assert body["path"] == "tools"
    assert body["max_total_matches"] == 25
    assert body["fixed_string"] is True


def test_source_read_preserves_content(monkeypatch, tmp_path):
    nia = reload_nia(monkeypatch, tmp_path)
    captured = capture_request(monkeypatch, b'{"content":"hello"}')

    result = decode_result(nia.nia_sources_tool({"action": "read", "source_id": "src123", "path": "docs/index.md", "line_start": 1, "line_end": 5}))

    assert result == {"content": "hello"}
    assert captured["method"] == "GET"
    assert captured["url"] == "https://apigcp.trynia.ai/v2/sources/src123/content?path=docs%2Findex.md&line_start=1&line_end=5"


def test_search_web_body(monkeypatch, tmp_path):
    nia = reload_nia(monkeypatch, tmp_path)
    captured = capture_request(monkeypatch, b'{"results":[]}')

    nia.nia_search_tool({"mode": "web", "query": "Hermes Agent", "num_results": 3, "category": "github"})
    body = json.loads(captured["data"].decode())

    assert captured["method"] == "POST"
    assert captured["url"] == "https://apigcp.trynia.ai/v2/search"
    assert body == {"mode": "web", "query": "Hermes Agent", "num_results": 3, "category": "github"}


def test_http_error_is_json_and_redacted(monkeypatch, tmp_path):
    nia = reload_nia(monkeypatch, tmp_path, env_key="secret-token")

    def fake_urlopen(req, timeout=0):
        raise HTTPError(req.full_url, 401, "Bearer secret-token rejected", Message(), io.BytesIO(b'{"error":"bad secret-token"}'))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = decode_result(nia.nia_usage_tool({}))

    assert result["http_status"] == 401
    assert "secret-token" not in json.dumps(result)
    assert "***" in json.dumps(result)


def test_toolset_contains_nia_tools():
    import toolsets

    assert "nia" in toolsets.TOOLSETS
    resolved = set(toolsets.resolve_toolset("nia"))
    assert {"nia_usage", "nia_repos", "nia_sources", "nia_search"}.issubset(resolved)
