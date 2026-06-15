#!/usr/bin/env python3
"""Read-only maestro-core grounding for the PROPOSER.

The reviewer gets maestro-core context via the maestro-docs MCP (LOOP_WITH_MCP,
inside the maestro-core harness). The PROPOSER — the step that edits the rubric —
had none. This gives it a bounded digest of how the codebase works, from EITHER:

  1. the LIVE maestro-docs MCP (if MAESTRO_DOCS_MCP_URL + _TOKEN are set), or
  2. maestro-core's own docs on disk, READ-ONLY (fallback),

so rubric edits cite real modules/patterns instead of generic advice.

Cardinal rules preserved: NEVER writes to maestro-core; tolerant of everything
being absent (returns a short note so the proposer still runs); and it NEVER logs
the MCP token. Secrets come from the gitignored .env / shell env, never code.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import config

# Read-only docs under MAESTRO_ROOT that describe how the codebase works.
_DOC_CANDIDATES = (
    "docs/ai-pr-review-architecture.md",
    "docs/architecture.md",
    "ARCHITECTURE.md",
    "docs/ARCHITECTURE.md",
    "README.md",
    "docs/README.md",
    ".github/workflows/auto-review.yml",
)
_NONE = "(no maestro-core context available — proposing without codebase grounding)"
_cache: dict[str, str] = {}


# --- local docs (fallback) ---------------------------------------------------
def _read_local_docs(max_chars: int) -> str:
    root = config.MAESTRO_ROOT
    files = [root / rel for rel in _DOC_CANDIDATES]
    try:
        files += sorted((root / "docs").glob("*.md"))
    except Exception:
        pass
    parts, seen, budget = [], set(), max_chars
    for p in files:
        if budget <= 0:
            break
        try:
            rp = p.resolve()
            if rp in seen or not p.exists() or not p.is_file():
                continue
            seen.add(rp)
            body = p.read_text(errors="ignore").strip()
            if not body:
                continue
            snippet = body[:budget]
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p.name
            parts.append(f"--- {rel} ---\n{snippet}")
            budget -= len(snippet)
        except Exception:
            continue
    return "\n\n".join(parts).strip()


# --- live MCP over Streamable HTTP (best-effort; falls back on any failure) ---
def _parse_rpc(body: str) -> dict:
    body = (body or "").strip()
    if not body:
        return {}
    if body[0] == "{":
        return json.loads(body)
    obj: dict = {}                               # SSE framing: last data: JSON wins
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            d = line[5:].strip()
            if d and d != "[DONE]":
                try:
                    obj = json.loads(d)
                except Exception:
                    pass
    return obj


def _rpc(url: str, token: str, session, method: str, params: dict, _id):
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    if _id is not None:
        msg["id"] = _id
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}"}      # token only ever in the header
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(msg).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        session = r.headers.get("Mcp-Session-Id") or session
        body = r.read().decode("utf-8", "replace")
    return (None if _id is None else _parse_rpc(body)), session


_INIT = {"protocolVersion": "2025-06-18", "capabilities": {},
         "clientInfo": {"name": "pr-reviewer-loop", "version": "1"}}


def _handshake(url: str, token: str):
    _, sid = _rpc(url, token, None, "initialize", _INIT, 1)
    try:
        _rpc(url, token, sid, "notifications/initialized", {}, None)
    except Exception:
        pass
    tl, sid = _rpc(url, token, sid, "tools/list", {}, 2)
    return ((tl or {}).get("result") or {}).get("tools") or [], sid


def _pick_tool(tools: list) -> dict | None:
    for kw in ("search", "query", "ask", "doc", "context", "retrieve", "lookup", "find"):
        for t in tools:
            if kw in (t.get("name") or "").lower():
                return t
    return tools[0] if tools else None


def _tool_args(tool: dict, query: str) -> dict:
    schema = (tool or {}).get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    cands = [k for k in required if (props.get(k) or {}).get("type") == "string"]
    if not cands:
        cands = [k for k, v in props.items() if (v or {}).get("type") == "string"]
    return {(cands[0] if cands else "query"): query}


def _mcp_fetch(url: str, token: str, tool_name: str, query: str, max_chars: int):
    try:
        tools, sid = _handshake(url, token)
        if not tools:
            return None
        chosen = (next((t for t in tools if t.get("name") == tool_name), None)
                  if tool_name else None) or _pick_tool(tools)
        if not chosen:
            return None
        cr, sid = _rpc(url, token, sid, "tools/call",
                       {"name": chosen.get("name"), "arguments": _tool_args(chosen, query)}, 3)
        content = ((cr or {}).get("result") or {}).get("content") or []
        texts = [c["text"] for c in content
                 if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
        out = "\n".join(texts).strip()
        return out[:max_chars] if out else None
    except Exception:
        return None                              # any failure → fall back to docs


def list_tools(url: str, token: str) -> list:
    """Diagnostic: what tools does the MCP expose? (so you can set the right one)"""
    try:
        return _handshake(url, token)[0]
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        return [{"name": "(http error)", "description": f"{e.code} {e.reason} — {body}"}]
    except Exception as e:  # noqa: BLE001
        return [{"name": "(error)", "description": str(e)[:200]}]


def diagnose(base_url: str, token: str) -> list:
    """Probe the configured URL and common MCP sub-paths so a 403/404 is legible:
    returns (url, status, body-snippet) for each — reveals token-vs-path issues."""
    data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": _INIT}).encode()
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}"}
    out = []
    for p in ("", "/mcp", "/sse", "/http", "/api/mcp"):
        u = base_url.rstrip("/") + p
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(u, data=data, headers=headers, method="POST"),
                    timeout=15) as r:
                out.append((u, r.status, r.read().decode("utf-8", "replace")[:160]))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")[:160]
            except Exception:
                body = ""
            out.append((u, e.code, body))
        except Exception as e:  # noqa: BLE001
            out.append((u, "ERR", str(e)[:120]))
    return out


# --- public API --------------------------------------------------------------
def load_context(max_chars: int | None = None) -> str:
    """Bounded, read-only codebase context for the proposer: live MCP if
    configured, else local docs. Cached per (root, mcp-url). Never raises."""
    if max_chars is None:
        max_chars = getattr(config, "MAESTRO_CONTEXT_CHARS", 8000)
    url = getattr(config, "MAESTRO_DOCS_MCP_URL", "")
    token = getattr(config, "MAESTRO_DOCS_MCP_TOKEN", "")
    key = f"{config.MAESTRO_ROOT}|{url}|{max_chars}"
    if key in _cache:
        return _cache[key]
    text = None
    if url and token:
        text = _mcp_fetch(url, token, getattr(config, "MAESTRO_DOCS_MCP_TOOL", ""),
                          getattr(config, "MAESTRO_DOCS_MCP_QUERY", ""), max_chars)
    if not text:
        text = _read_local_docs(max_chars)
    text = (text or "").strip() or _NONE
    _cache[key] = text
    return text


def summary() -> dict:
    """For telemetry / the dashboard footer."""
    if not getattr(config, "MAESTRO_CONTEXT", False):
        return {"enabled": False, "source": "off", "chars": 0}
    mcp = bool(getattr(config, "MAESTRO_DOCS_MCP_URL", "")
               and getattr(config, "MAESTRO_DOCS_MCP_TOKEN", ""))
    txt = load_context()
    return {"enabled": True, "mcp_configured": mcp,
            "grounded": txt != _NONE, "chars": len(txt) if txt != _NONE else 0}


if __name__ == "__main__":
    import sys
    if "--tools" in sys.argv:
        u, t = config.MAESTRO_DOCS_MCP_URL, config.MAESTRO_DOCS_MCP_TOKEN
        if not (u and t):
            print("Set MAESTRO_DOCS_MCP_URL + MAESTRO_DOCS_MCP_TOKEN (in .env) first.")
        else:
            tools = list_tools(u, t)
            if tools and not str(tools[0].get("name", "")).startswith("("):
                print(f"maestro-docs MCP tools at {u}:")
                for tool in tools:
                    print(f"  - {tool.get('name', '?')}: {(tool.get('description') or '')[:90]}")
            else:
                print(f"Could not reach the MCP at {u}:\n  {tools[0].get('description')}\n")
                print("Probing endpoint paths (status · url · body):")
                for url, status, body in diagnose(u, t):
                    print(f"  {str(status):>4}  {url}")
                    if body:
                        print(f"        {body}")
                print("\n→ If one path returns 200/JSON, put THAT full URL in "
                      "MAESTRO_DOCS_MCP_URL (.env).")
                print("→ If every path is 401/403 with an auth message, it's the token.")
    else:
        print(load_context())
