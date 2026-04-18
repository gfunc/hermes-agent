# WeCom MCP Architecture Discussion — Where Should Features Live in Hermes?

> Context: User does NOT want to enhance `tools/mcp_tool.py`. We need to find the right home for OpenClaw-level WeCom MCP capabilities.

## 1. Understanding the Two Architectures

### OpenClaw Plugin Architecture (from plugin-architect analysis)

```
WeCom WS ──> monitor.ts ──> OpenClaw Core ──> LLM ──> wecom_mcp tool ──> src/mcp/tool.ts
                                                              │
                                                              ▼
                                                    interceptors/ (biz-error, msg-media, ...)
                                                              │
                                                              ▼
                                                    transport.ts (Streamable HTTP, session mgmt)
                                                              │
                                                              ▼
                                                    MCP Server (HTTP)
```

Key insight: **MCP is completely decoupled from the channel**. The channel pumps messages; the tool is invoked by the LLM through the framework's tool-use mechanism.

The plugin registers **one generic tool** (`wecom_mcp`) and uses **15 skill prompt documents** to guide the LLM. Skills are markdown files, not code.

### Hermes Current Architecture (from own exploration + gap analysis)

```
WeCom WS ──> gateway/platforms/wecom/adapter.py ──> gateway/run.py ──> LLM
                                                              │
                                    (discovers URLs only)     │
                                                              ▼
                                                    tools/mcp_tool.py
                                                              │
                                                    (generic stdio/HTTP MCP)
                                                              │
                                                              ▼
                                                    MCP Server
```

Hermes registers **one MCP tool per server** via `tools/mcp_tool.py`. Each server's tools are dynamically discovered and registered individually.

The WeCom adapter only discovers `{category: url}` and passes to `register_mcp_servers()`. The generic MCP client then connects to those URLs.

---

## 2. Integration Options (Without Modifying `tools/mcp_tool.py`)

### Option A: New Tool Module `tools/wecom_mcp_tool.py` — **RECOMMENDED**

Create a standalone tool module that implements the full OpenClaw-style MCP client.

**What it would contain:**
- A single `wecom_mcp` tool with `list`/`call` actions (mirrors OpenClaw)
- Streamable HTTP transport with session lifecycle
- Schema cleaning (`clean_schema_for_gemini()`)
- Interceptor pipeline (4 interceptors: biz-error, msg-media, smartpage-create, smartpage-export)
- Config discovery via WeCom WS (`mcp_get_config`)

**How it integrates:**
```python
# tools/wecom_mcp_tool.py
import registry

# Self-register at import time
registry.register(
    name="wecom_mcp",
    toolset="wecom",
    schema={...},  # action, category, method, args
    handler=handle_wecom_mcp,
    is_async=True,
)
```

**Files to create/modify:**
| File | Purpose | New/Modify |
|------|---------|------------|
| `tools/wecom_mcp_tool.py` | Main tool definition, handler | **New** |
| `tools/wecom_mcp/` | Sub-package for transport, interceptors, schema | **New dir** |
| `tools/wecom_mcp/transport.py` | Streamable HTTP client, session mgmt | **New** |
| `tools/wecom_mcp/interceptors.py` | Interceptor pipeline | **New** |
| `tools/wecom_mcp/schema.py` | Schema cleaning for model compatibility | **New** |
| `gateway/platforms/wecom/adapter.py` | Minor: expose `get_mcp_configs()` or WS client for config fetch | Modify |

**Pros:**
- Clean separation from generic MCP — zero coupling to `tools/mcp_tool.py`
- Follows Hermes tool registration pattern (`registry.register()`)
- Follows OpenClaw's proven "one tool + skills" model
- Can be conditionally loaded (skip if no WeCom config)
- Skills/prompts can be added as system prompt injection (no skill system needed)

**Cons:**
- Duplicates HTTP client code that `tools/mcp_tool.py` already has
- Needs access to WeCom WS client for config discovery
- No Hermes "skill" system — prompts would be system context or hardcoded

---

### Option B: WeCom Platform Extension `gateway/platforms/wecom/mcp_client.py`

Put the MCP client inside the WeCom platform package itself.

**What it would contain:**
- Streamable HTTP transport module in `gateway/platforms/wecom/mcp/`
- The WeCom adapter exposes a `get_mcp_client()` method
- A thin tool wrapper in `tools/` that delegates to the platform client

**Architecture:**
```
gateway/platforms/wecom/
  ├── adapter.py       (exposes get_mcp_configs(), get_ws_client())
  ├── mcp/
  │   ├── __init__.py
  │   ├── transport.py (Streamable HTTP client)
  │   ├── session.py   (Session lifecycle)
  │   └── interceptors.py
  └── ...

tools/wecom_mcp_tool.py (thin wrapper)
```

**Pros:**
- MCP is naturally WeCom-specific — lives with its platform
- Platform already has WS client, auth, config discovery
- Keeps WeCom concerns in one place

**Cons:**
- Platform package now depends on tool registry (circular concern)
- Harder to test in isolation
- Violates separation of transport vs. tool-use layers
- Platform shouldn't know about LLM tool interfaces

---

### Option C: Standalone Package `mcp_clients/wecom_streamable_http.py`

Create a separate top-level module for WeCom MCP client, independent of both `tools/` and `gateway/`.

**Architecture:**
```
mcp_clients/
  ├── __init__.py
  ├── wecom/
  │   ├── __init__.py
  │   ├── transport.py
  │   ├── session.py
  │   ├── interceptors.py
  │   └── schema.py
  └── ...

tools/wecom_mcp_tool.py (imports from mcp_clients.wecom)
gateway/platforms/wecom/adapter.py (imports from mcp_clients.wecom for config fetch)
```

**Pros:**
- Most decoupled — reusable outside Hermes
- Clean layering: transport → tool → platform
- Can be tested independently

**Cons:**
- More files, more imports
- Overkill for a single platform's MCP needs
- No other MCP clients exist to justify a top-level `mcp_clients/` package
- Hermes doesn't have this pattern currently

---

### Option D: Enhance Gateway's MCP Discovery Only

Keep the current architecture but make `gateway/run.py:8661` do more than just URL passing.

**What would change:**
- Instead of `register_mcp_servers({category: url})`, the gateway wraps URLs with a custom HTTP transport before registering
- But this STILL goes through `tools/mcp_tool.py` — which the user wants to avoid

**Verdict: Not viable** — any path through `register_mcp_servers()` ends up in `tools/mcp_tool.py`.

---

### Option E: Custom MCP Server Shim

Create a local stdio-based MCP server that wraps WeCom's HTTP MCP servers.

**Architecture:**
```
Hermes ──> tools/mcp_tool.py ──> stdio process "wecom-mcp-shim"
                                      │
                                      ▼
                           Streamable HTTP client
                                      │
                                      ▼
                           WeCom MCP Server (HTTP)
```

The shim is a separate Python script or Node.js process that:
1. Accepts stdio MCP protocol from Hermes
2. Translates to Streamable HTTP for WeCom
3. Handles session lifecycle, interceptors, schema cleaning

**Pros:**
- Reuses `tools/mcp_tool.py` (stdio transport) without modifying it
- Hermes sees it as just another MCP server
- Clean protocol boundary

**Cons:**
- Running an extra process is heavy
- Process management complexity
- Overhead for every MCP call (IPC + HTTP)
- The user said they don't want to enhance `tools/mcp_tool.py`, but this uses it indirectly

---

## 3. Recommendation: Option A + Prompt Injection Pattern

### Why Option A Wins

1. **Zero modification to `tools/mcp_tool.py`** — Complete separation
2. **Follows Hermes patterns** — Uses existing `registry.register()` mechanism
3. **Follows OpenClaw's proven model** — One generic tool + prompt-driven usage
4. **Minimal blast radius** — Changes are additive, not modificatory
5. **No architectural layering violations** — Transport in tool module, platform stays pure

### Implementation Sketch

```python
# tools/wecom_mcp_tool.py
"""WeCom MCP tool — Streamable HTTP client for WeCom MCP Servers.

Implements the full Streamable HTTP protocol with session lifecycle,
auto-rebuild on 404, schema cleaning, and business interceptors.

Usage (by LLM):
    wecom_mcp list <category>     # e.g., wecom_mcp list contact
    wecom_mcp call <category> <method> '<json_args>'
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import aiohttp

from tools.registry import registry
from gateway.platforms.wecom.adapter import WeComAdapter  # for config fetch

logger = logging.getLogger(__name__)

# ── Tool Registration ─────────────────────────────────────────────────────

registry.register(
    name="wecom_mcp",
    toolset="wecom",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "call"]},
            "category": {"type": "string", "description": "MCP category: contact, doc, msg, ..."},
            "method": {"type": "string", "description": "Method name (for call action)"},
            "args": {"type": ["string", "object"], "description": "JSON args (for call action)"},
        },
        "required": ["action", "category"],
    },
    handler=handle_wecom_mcp,
    is_async=True,
    description="Call WeCom MCP servers directly via Streamable HTTP protocol",
)

# ── Transport Layer ───────────────────────────────────────────────────────

# In-memory caches (mirrors OpenClaw)
_mcp_config_cache: Dict[str, dict] = {}
_mcp_session_cache: Dict[str, McpSession] = {}
_stateless_categories: set[str] = set()
_inflight_init: Dict[str, asyncio.Future] = {}

class McpSession:
    def __init__(self):
        self.session_id: Optional[str] = None
        self.initialized = False
        self.stateless = False

async def send_json_rpc(category: str, method: str, params: Optional[dict] = None, 
                        timeout_ms: int = 30000) -> Any:
    """Send JSON-RPC to WeCom MCP Server with full session management."""
    url = await _get_mcp_url(category)
    session = await _get_or_create_session(url, category)
    
    body = {
        "jsonrpc": "2.0",
        "id": generate_req_id("mcp_rpc"),
        "method": method,
    }
    if params is not None:
        body["params"] = params
    
    try:
        return await _send_raw_json_rpc(url, session, body, timeout_ms)
    except McpHttpError as e:
        if e.status_code == 404 and not session.stateless:
            # Session expired — rebuild and retry once
            _mcp_session_cache.pop(category, None)
            session = await _rebuild_session(url, category)
            return await _send_raw_json_rpc(url, session, body, timeout_ms)
        raise

# ... (interceptors, schema cleaning, etc.)

# ── Handler ───────────────────────────────────────────────────────────────

async def handle_wecom_mcp(action: str, category: str, method: str = "", 
                           args: Optional[str | dict] = None) -> str:
    if action == "list":
        result = await send_json_rpc(category, "tools/list")
        tools = result.get("tools", [])
        # Clean schemas for model compatibility
        for tool in tools:
            if "inputSchema" in tool:
                tool["inputSchema"] = clean_schema_for_gemini(tool["inputSchema"])
        return json.dumps({"category": category, "tools": tools}, indent=2)
    
    elif action == "call":
        parsed_args = json.loads(args) if isinstance(args, str) else (args or {})
        
        # Run before-call interceptors
        ctx = {"category": category, "method": method, "args": parsed_args}
        resolved = await resolve_before_call(ctx)
        final_args = resolved.get("args", parsed_args)
        timeout_ms = resolved.get("timeout_ms", 30000)
        
        result = await send_json_rpc(category, "tools/call", {
            "name": method,
            "arguments": final_args,
        }, timeout_ms)
        
        # Run after-call interceptors
        result = await run_after_call(ctx, result)
        return json.dumps(result, indent=2)
    
    raise ValueError(f"Unknown action: {action}")
```

### How Prompts/Skills Work Without a Skill System

Since Hermes doesn't have OpenClaw's skill system, we can inject WeCom MCP guidance into the system prompt when the active channel is WeCom:

```python
# In gateway/run.py or a WeCom-specific prompt builder
if active_platform == "wecom":
    system_prompt += WECom_MCP_PROMPT  # ~2KB of usage guidance
```

Or add it as a "tool hint" in the WeCom adapter that gets prepended to the first user message:

```python
# gateway/platforms/wecom/adapter.py
WECom_MCP_HINT = """
You have access to WeCom MCP tools via `wecom_mcp`:
- wecom_mcp list contact  → list available tools in a category
- wecom_mcp call contact get_userlist '{}'  → call a specific tool

Common categories: contact, doc, msg, meeting, todo, schedule
""".strip()
```

### Interceptor Mapping from OpenClaw to Python

| OpenClaw (TypeScript) | Hermes (Python) | What it does |
|-----------------------|-----------------|--------------|
| `biz-error.ts` | `interceptors/biz_error.py` | Parse result for `errcode: 850002`, clear cache |
| `msg-media.ts` | `interceptors/msg_media.py` | `get_msg_media` → decode base64, save to file, return `local_path` |
| `smartpage-create.ts` | `interceptors/smartpage_create.py` | Read local files into `page_content` before call |
| `smartpage-export.ts` | `interceptors/smartpage_export.py` | Save markdown content to local file |

---

## 4. What Would Need to Happen

### Phase 1: Core Transport (1-2 days)
1. Create `tools/wecom_mcp/` package
2. Implement `transport.py` with Streamable HTTP, session lifecycle, auto-rebuild
3. Implement `schema.py` with schema cleaning for Gemini/Claude compatibility
4. Create `tools/wecom_mcp_tool.py` with basic `list`/`call` handler

### Phase 2: Interceptors (1 day)
1. Implement interceptor pipeline (`interceptors/__init__.py`)
2. Port 4 interceptors from OpenClaw
3. Add performance logging (matching OpenClaw's detailed timing)

### Phase 3: Integration (0.5 day)
1. Add WeCom MCP prompt injection to system context when channel is WeCom
2. Test with actual WeCom MCP servers
3. Add to WeCom test suite

### Files Changed Summary

| File | Action | Lines |
|------|--------|-------|
| `tools/wecom_mcp_tool.py` | Create | ~50 |
| `tools/wecom_mcp/__init__.py` | Create | ~10 |
| `tools/wecom_mcp/transport.py` | Create | ~300 |
| `tools/wecom_mcp/schema.py` | Create | ~120 |
| `tools/wecom_mcp/interceptors/__init__.py` | Create | ~100 |
| `tools/wecom_mcp/interceptors/biz_error.py` | Create | ~40 |
| `tools/wecom_mcp/interceptors/msg_media.py` | Create | ~80 |
| `tools/wecom_mcp/interceptors/smartpage_create.py` | Create | ~70 |
| `tools/wecom_mcp/interceptors/smartpage_export.py` | Create | ~60 |
| `gateway/platforms/wecom/adapter.py` | Modify (minor) | ~5 |
| `gateway/run.py` | Modify (prompt injection) | ~10 |
| **Total** | | **~845 lines new** |

---

## 5. Comparison with OpenClaw

| Aspect | OpenClaw Plugin | Proposed Hermes Implementation |
|--------|-----------------|--------------------------------|
| Tool registration | `api.registerTool()` | `registry.register()` |
| Skills | Markdown files in `skills/` | System prompt injection |
| Transport | Custom HTTP via `fetch()` | `aiohttp` |
| Session cache | `Map<string, McpSession>` | `dict[str, McpSession]` |
| Interceptors | TypeScript interfaces | Python protocols/callables |
| Schema cleaning | `cleanSchemaForGemini()` | `clean_schema_for_gemini()` |
| Config fetch | SDK `WSClient.reply()` | Direct `_send_request()` on adapter |
| Language | TypeScript | Python |

---

## 6. Open Questions

1. **Should we keep Hermes' current generic MCP registration too?** The WeCom adapter currently passes URLs to `register_mcp_servers()`. If we add `wecom_mcp`, we should disable the generic registration for WeCom to avoid duplicate tools.

2. **How do skills/prompts get updated?** OpenClaw skills are versioned with the plugin. Hermes would need a mechanism to update system prompts (config reload, git pull, etc.).

3. **Testing strategy?** The OpenClaw plugin has no visible tests. Hermes should add mock MCP server tests, similar to the existing `mock_server.py` for WeCom WS.

4. **Should we abstract the interceptor pattern?** The interceptor pipeline (`beforeCall`/`afterCall`) is generic enough to be useful for other tools. Should it live in `tools/` as a shared utility?
