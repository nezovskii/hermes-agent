"""Nia retrieval tools.

Native Hermes toolset for Nia (https://trynia.ai) repositories,
documentation sources, and web/deep search.  Credentials are resolved from
``NIA_API_KEY`` first, then ``~/.config/nia/api_key``.  Secrets are never
included in returned errors.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

BASE_URL = "https://apigcp.trynia.ai/v2"
_TIMEOUT_SECONDS = 45

_SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)([^\s,;\"'}]+)", re.IGNORECASE),
    re.compile(r"\b(api[_-]?key|auth[_-]?token|access[_-]?token|token)\s*[=:]\s*([^\s,;\"'}]+)", re.IGNORECASE),
]


def _redact(text: Any) -> str:
    value = str(text)
    key = _get_api_key()
    if key:
        value = value.replace(key, "***")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda m: f"{m.group(1)}***", value)
    return value


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _error(message: str, http_status: Optional[int] = None) -> str:
    payload: Dict[str, Any] = {"error": _redact(message)}
    if http_status is not None:
        payload["http_status"] = http_status
    return _json(payload)


def _get_api_key() -> str:
    env_key = os.getenv("NIA_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        return (Path.home() / ".config" / "nia" / "api_key").read_text().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _check_nia_available() -> bool:
    return bool(_get_api_key())


def _quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def _query(params: Dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    return urllib.parse.urlencode(clean)


def _request(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
    key = _get_api_key()
    if not key:
        return {"error": "Nia API key not configured. Set NIA_API_KEY or ~/.config/nia/api_key."}

    url = path if path.startswith("http") else f"{BASE_URL}{path}"
    data = None
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw.strip():
                return {"success": True}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"content": raw}
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(exc)
        try:
            parsed = json.loads(raw) if raw else {}
            if isinstance(parsed, dict):
                msg = parsed.get("error") or parsed.get("message") or raw or exc.reason
            else:
                msg = raw or exc.reason
        except json.JSONDecodeError:
            msg = raw or exc.reason
        return {"error": _redact(msg), "http_status": exc.code}
    except Exception as exc:  # pragma: no cover - defensive around network stack
        return {"error": _redact(f"Nia request failed: {type(exc).__name__}: {exc}")}


def _bool_arg(args: Dict[str, Any], name: str) -> Optional[bool]:
    if name not in args or args.get(name) in (None, ""):
        return None
    value = args.get(name)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _int_arg(args: Dict[str, Any], name: str, default: Optional[int] = None) -> Optional[int]:
    value = args.get(name, default)
    if value in (None, ""):
        return None
    return int(value)


def _grep_body(args: Dict[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "pattern": args.get("pattern", ""),
        "context_lines": _int_arg(args, "context_lines", 3),
        "max_total_matches": _int_arg(args, "max_total", 50),
    }
    if args.get("path"):
        body["path"] = args["path"]
    for key in ("case_sensitive", "whole_word", "fixed_string", "highlight", "exhaustive"):
        val = _bool_arg(args, key)
        if val is not None:
            body[key] = val
    if args.get("output_mode"):
        body["output_mode"] = args["output_mode"]
    for in_key, out_key in (("lines_after", "A"), ("lines_before", "B"), ("max_per_file", "max_matches_per_file")):
        val = _int_arg(args, in_key)
        if val is not None:
            body[out_key] = val
    return {k: v for k, v in body.items() if v is not None}


def nia_usage_tool(args: Dict[str, Any], **kw) -> str:
    """Return Nia account usage/limits."""
    return _json(_request("GET", "/usage"))


def nia_repos_tool(args: Dict[str, Any], **kw) -> str:
    action = str(args.get("action", "list")).strip().lower()
    repository = str(args.get("repository", "")).strip()

    if action == "list":
        return _json(_request("GET", "/sources?type=repository"))

    if action == "index":
        if not repository:
            return _error("repository is required for action=index")
        body: Dict[str, Any] = {"type": "repository", "repository": repository}
        if args.get("ref"):
            body["ref"] = args["ref"]
        if args.get("display_name"):
            body["display_name"] = args["display_name"]
        add_global = _bool_arg(args, "add_global")
        if add_global is not None:
            body["add_as_global_source"] = add_global
        return _json(_request("POST", "/sources", body))

    if not repository:
        return _error(f"repository is required for action={action}")
    rid = _quote(repository)

    if action == "status":
        return _json(_request("GET", f"/sources/{rid}?type=repository"))
    if action == "tree":
        params = _query({
            "type": "repository",
            "branch": args.get("ref") or args.get("branch"),
            "include_paths": args.get("include_paths"),
            "exclude_paths": args.get("exclude_paths"),
            "file_extensions": args.get("file_extensions"),
            "exclude_extensions": args.get("exclude_extensions"),
            "show_full_paths": _bool_arg(args, "show_full_paths"),
        })
        return _json(_request("GET", f"/sources/{rid}/tree" + (f"?{params}" if params else "")))
    if action == "read":
        if not args.get("path"):
            return _error("path is required for action=read")
        params = _query({"type": "repository", "path": args.get("path"), "ref": args.get("ref")})
        return _json(_request("GET", f"/sources/{rid}/content?{params}"))
    if action == "grep":
        if not args.get("pattern"):
            return _error("pattern is required for action=grep")
        body = _grep_body(args)
        if args.get("ref"):
            body["ref"] = args["ref"]
        return _json(_request("POST", f"/sources/{rid}/grep", body))
    if action == "delete":
        return _json(_request("DELETE", f"/sources/{rid}?type=repository"))
    if action == "rename":
        if not args.get("display_name"):
            return _error("display_name is required for action=rename")
        return _json(_request("PATCH", f"/sources/{rid}?type=repository", {"display_name": args["display_name"]}))

    return _error(f"Unsupported nia_repos action: {action}")


def nia_sources_tool(args: Dict[str, Any], **kw) -> str:
    action = str(args.get("action", "list")).strip().lower()
    source_id = str(args.get("source_id") or args.get("identifier") or "").strip()
    source_type = args.get("type")

    if action == "list":
        qs = _query({"type": source_type})
        return _json(_request("GET", "/sources" + (f"?{qs}" if qs else "")))
    if action == "index":
        url = str(args.get("url", "")).strip()
        if not url:
            return _error("url is required for action=index")
        body: Dict[str, Any] = {
            "type": "documentation",
            "url": url,
            "limit": _int_arg(args, "limit", 1000),
            "only_main_content": _bool_arg(args, "only_main_content") if _bool_arg(args, "only_main_content") is not None else True,
        }
        for key in ("display_name", "focus", "url_patterns", "exclude_patterns", "llms_txt_strategy"):
            if args.get(key):
                out_key = "focus_instructions" if key == "focus" else key
                body[out_key] = args[key]
        for key in ("extract_branding", "extract_images", "is_pdf", "check_llms_txt", "include_screenshot", "add_global"):
            val = _bool_arg(args, key)
            if val is not None:
                out_key = "add_as_global_source" if key == "add_global" else key
                body[out_key] = val
        for key in ("max_depth", "wait_for", "max_age"):
            val = _int_arg(args, key)
            if val is not None:
                body[key] = val
        return _json(_request("POST", "/sources", {k: v for k, v in body.items() if v is not None}))
    if action == "resolve":
        identifier = str(args.get("identifier") or source_id).strip()
        if not identifier:
            return _error("identifier is required for action=resolve")
        qs = _query({"identifier": identifier, "type": source_type})
        return _json(_request("GET", f"/sources/resolve?{qs}"))

    if not source_id:
        return _error(f"source_id or identifier is required for action={action}")
    sid = _quote(source_id)
    type_qs = _query({"type": source_type})
    type_suffix = f"?{type_qs}" if type_qs else ""

    if action == "get":
        return _json(_request("GET", f"/sources/{sid}{type_suffix}"))
    if action == "sync":
        return _json(_request("POST", f"/sources/{sid}/sync{type_suffix}", {}))
    if action == "tree":
        return _json(_request("GET", f"/sources/{sid}/tree"))
    if action == "ls":
        qs = _query({"path": args.get("path", "/")})
        return _json(_request("GET", f"/sources/{sid}/tree?{qs}"))
    if action == "read":
        if not args.get("path"):
            return _error("path is required for action=read")
        qs = _query({
            "path": args.get("path"),
            "line_start": args.get("line_start"),
            "line_end": args.get("line_end"),
            "max_length": args.get("max_length"),
        })
        return _json(_request("GET", f"/sources/{sid}/content?{qs}"))
    if action == "grep":
        if not args.get("pattern"):
            return _error("pattern is required for action=grep")
        return _json(_request("POST", f"/sources/{sid}/grep", _grep_body(args)))
    if action == "delete":
        return _json(_request("DELETE", f"/sources/{sid}{type_suffix}"))
    if action == "rename":
        if not args.get("display_name"):
            return _error("display_name is required for action=rename")
        return _json(_request("PATCH", f"/sources/{sid}", {"display_name": args["display_name"]}))

    return _error(f"Unsupported nia_sources action: {action}")


def _csv_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def nia_search_tool(args: Dict[str, Any], **kw) -> str:
    mode = str(args.get("mode", "query")).strip().lower()
    query = str(args.get("query", "")).strip()
    if not query:
        return _error("query is required")

    if mode == "web":
        body: Dict[str, Any] = {"mode": "web", "query": query, "num_results": _int_arg(args, "num_results", 5)}
        for key in ("category", "find_similar_to"):
            if args.get(key):
                body[key] = args[key]
        if args.get("days_back") not in (None, ""):
            body["days_back"] = _int_arg(args, "days_back")
        return _json(_request("POST", "/search", body))

    if mode == "deep":
        body = {"mode": "deep", "query": query}
        if args.get("output_format"):
            body["output_format"] = args["output_format"]
        if _bool_arg(args, "verbose") is not None:
            body["verbose"] = _bool_arg(args, "verbose")
        return _json(_request("POST", "/search", body))

    if mode == "universal":
        body = {
            "mode": "universal",
            "query": query,
            "top_k": _int_arg(args, "top_k", 20),
            "include_repos": _bool_arg(args, "include_repos") if _bool_arg(args, "include_repos") is not None else True,
            "include_docs": _bool_arg(args, "include_docs") if _bool_arg(args, "include_docs") is not None else True,
            "compress_output": _bool_arg(args, "compress_output") if _bool_arg(args, "compress_output") is not None else False,
        }
        return _json(_request("POST", "/search", body))

    if mode == "query":
        repos = _csv_list(args.get("repos") or args.get("repositories"))
        docs = _csv_list(args.get("docs") or args.get("data_sources"))
        body = {
            "mode": "query",
            "messages": [{"role": "user", "content": query}],
            "repositories": [{"repository": repo} for repo in repos],
            "data_sources": docs,
            "search_mode": args.get("search_mode") or ("repositories" if repos and not docs else "sources" if docs and not repos else "unified"),
            "stream": False,
            "include_sources": True,
        }
        for key in ("category", "reasoning_strategy", "model"):
            if args.get(key):
                body[key] = args[key]
        for key in ("max_tokens",):
            if args.get(key) not in (None, ""):
                body[key] = _int_arg(args, key)
        for key in ("fast_mode", "skip_llm", "bypass_semantic_cache", "include_follow_ups"):
            val = _bool_arg(args, key)
            if val is not None:
                body[key] = val
        return _json(_request("POST", "/search", body))

    return _error(f"Unsupported nia_search mode: {mode}")


NIA_USAGE_SCHEMA = {
    "name": "nia_usage",
    "description": "Show Nia account tier and usage limits. Use before expensive deep/oracle research.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

NIA_REPOS_SCHEMA = {
    "name": "nia_repos",
    "description": "Manage and search Nia-indexed GitHub repositories. Defaults to private indexing; pass add_global=true only when explicitly requested.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "index", "status", "tree", "grep", "read", "delete", "rename"]},
            "repository": {"type": "string", "description": "GitHub owner/repo or Nia source identifier."},
            "ref": {"type": "string", "description": "Branch, tag, or commit ref."},
            "display_name": {"type": "string"},
            "add_global": {"type": "boolean", "description": "Opt in to public/global source indexing."},
            "path": {"type": "string", "description": "File path or path prefix."},
            "pattern": {"type": "string", "description": "Regex or fixed string for grep."},
            "max_total": {"type": "integer"},
            "fixed_string": {"type": "boolean"},
            "case_sensitive": {"type": "boolean"},
            "whole_word": {"type": "boolean"},
        },
        "required": ["action"],
    },
}

NIA_SOURCES_SCHEMA = {
    "name": "nia_sources",
    "description": "Manage and search Nia-indexed documentation sources, PDFs, and URLs. Hermes defaults to private indexing unless add_global=true is provided.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "index", "get", "resolve", "sync", "tree", "ls", "grep", "read", "delete", "rename"]},
            "source_id": {"type": "string"},
            "identifier": {"type": "string", "description": "Name, URL, or source identifier for resolve/get operations."},
            "type": {"type": "string", "description": "Optional Nia source type filter."},
            "url": {"type": "string", "description": "URL to index."},
            "limit": {"type": "integer"},
            "display_name": {"type": "string"},
            "focus": {"type": "string"},
            "add_global": {"type": "boolean"},
            "path": {"type": "string"},
            "pattern": {"type": "string"},
            "line_start": {"type": "integer"},
            "line_end": {"type": "integer"},
            "max_length": {"type": "integer"},
            "max_total": {"type": "integer"},
            "fixed_string": {"type": "boolean"},
        },
        "required": ["action"],
    },
}

NIA_SEARCH_SCHEMA = {
    "name": "nia_search",
    "description": "Search with Nia. Use mode=query for indexed repos/docs, web for current web, deep for synthesized research, universal for broad indexed-source retrieval.",
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["query", "web", "deep", "universal"]},
            "query": {"type": "string"},
            "repos": {"type": "string", "description": "Comma-separated repositories for mode=query."},
            "docs": {"type": "string", "description": "Comma-separated source IDs/URLs for mode=query."},
            "num_results": {"type": "integer"},
            "top_k": {"type": "integer"},
            "category": {"type": "string"},
            "days_back": {"type": "integer"},
            "output_format": {"type": "string"},
            "max_tokens": {"type": "integer"},
            "fast_mode": {"type": "boolean"},
            "skip_llm": {"type": "boolean"},
        },
        "required": ["query"],
    },
}


from tools.registry import registry

registry.register(
    name="nia_usage",
    toolset="nia",
    schema=NIA_USAGE_SCHEMA,
    handler=nia_usage_tool,
    check_fn=_check_nia_available,
    emoji="🧠",
)
registry.register(
    name="nia_repos",
    toolset="nia",
    schema=NIA_REPOS_SCHEMA,
    handler=nia_repos_tool,
    check_fn=_check_nia_available,
    emoji="🧠",
)
registry.register(
    name="nia_sources",
    toolset="nia",
    schema=NIA_SOURCES_SCHEMA,
    handler=nia_sources_tool,
    check_fn=_check_nia_available,
    emoji="🧠",
)
registry.register(
    name="nia_search",
    toolset="nia",
    schema=NIA_SEARCH_SCHEMA,
    handler=nia_search_tool,
    check_fn=_check_nia_available,
    emoji="🧠",
)
