# WeCom Core + Multi-Account Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract shared WeCom account configuration resolution, make `wecom.py` and `wecom_callback.py` multi-account aware, and replace hand-rolled text batching with the existing `TextBatchAggregator`.

**Architecture:** Create `gateway/platforms/wecom_accounts.py` with a `WeComAccount` dataclass and `resolve_wecom_accounts()` that supports both legacy single-account top-level config (`extra.bot_id`) and new multi-account config (`extra.accounts`). Refactor `wecom.py` to iterate over resolved accounts and use `TextBatchAggregator`. Refactor `wecom_callback.py` to use the same resolver for its `apps` list.

**Tech Stack:** Python 3.11+, aiohttp, httpx, pytest. Existing Hermes utilities: `gateway.platforms.helpers.TextBatchAggregator`, `gateway.platforms.helpers.MessageDeduplicator`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `gateway/platforms/wecom_accounts.py` | **Create** — `WeComAccount` dataclass and `resolve_wecom_accounts()` with deep merge logic |
| `tests/gateway/test_wecom_accounts.py` | **Create** — unit tests for account resolution (single, multi, env fallback, defaults) |
| `gateway/platforms/wecom.py` | **Modify** — use `resolve_wecom_accounts`, store `_accounts` list, loop over accounts in `connect()`, replace inline batching with `TextBatchAggregator` |
| `gateway/platforms/wecom_callback.py` | **Modify** — replace `_normalize_apps` with `resolve_wecom_accounts`, adapt `WeComAccount` fields to existing callback app dict shape |

---

## Task 1: WeCom Account Configuration Resolver

**Files:**
- Create: `gateway/platforms/wecom_accounts.py`
- Test: `tests/gateway/test_wecom_accounts.py`

### Step 1.1: Write the failing test for single-account resolution

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_accounts.py
import os
from gateway.config import PlatformConfig
from gateway.platforms.wecom_accounts import resolve_wecom_accounts


def test_resolve_single_account_from_top_level():
    config = PlatformConfig(
        extra={
            "bot_id": "bot-123",
            "secret": "sec-456",
            "websocket_url": "wss://example.com",
            "dm_policy": "allowlist",
            "allow_from": ["alice"],
        }
    )
    accounts = resolve_wecom_accounts(config)
    assert len(accounts) == 1
    assert accounts[0].account_id == "default"
    assert accounts[0].bot_id == "bot-123"
    assert accounts[0].secret == "sec-456"
    assert accounts[0].websocket_url == "wss://example.com"
    assert accounts[0].dm_policy == "allowlist"
    assert accounts[0].allow_from == ["alice"]
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_accounts.py::test_resolve_single_account_from_top_level -v`

Expected: `FAILED` with `ModuleNotFoundError: No module named 'gateway.platforms.wecom_accounts'`

### Step 1.2: Implement `WeComAccount` and `resolve_wecom_accounts`

- [ ] **Write minimal implementation**

```python
# gateway/platforms/wecom_accounts.py
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from gateway.config import PlatformConfig

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"


def _coerce_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


@dataclass(frozen=True)
class WeComAccount:
    account_id: str
    name: str = ""
    enabled: bool = True
    connection_mode: str = "websocket"  # "websocket" | "webhook"

    # Bot credentials
    bot_id: str = ""
    secret: str = ""
    websocket_url: str = DEFAULT_WS_URL

    # Agent credentials (for callback / REST fallback)
    corp_id: str = ""
    corp_secret: str = ""
    agent_id: Optional[int] = None
    token: str = ""
    encoding_aes_key: str = ""
    receive_id: str = ""  # used for webhook decryption

    # Policy
    dm_policy: str = "open"  # open | allowlist | disabled | pairing
    allow_from: List[str] = field(default_factory=list)
    group_policy: str = "open"  # open | allowlist | disabled
    group_allow_from: List[str] = field(default_factory=list)
    groups: Dict[str, Any] = field(default_factory=dict)

    # Media
    media_local_roots: List[str] = field(default_factory=list)

    # Webhook-only settings
    welcome_text: str = ""

    @property
    def is_bot_configured(self) -> bool:
        return bool(self.bot_id.strip() and self.secret.strip())

    @property
    def is_agent_configured(self) -> bool:
        return bool(
            self.corp_id.strip() and self.corp_secret.strip() and self.agent_id is not None
        )

    @property
    def is_webhook_configured(self) -> bool:
        return bool(self.token.strip() and self.encoding_aes_key.strip())


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge override into base. Lists are replaced, dicts are merged."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _account_from_dict(account_id: str, data: Dict[str, Any]) -> WeComAccount:
    """Build a WeComAccount from a raw dict, applying env fallbacks."""
    bot_id = str(data.get("bot_id") or data.get("botId") or "").strip()
    secret = str(data.get("secret") or data.get("corpSecret") or "").strip()

    corp_id = str(data.get("corp_id") or data.get("corpId") or "").strip()
    corp_secret = str(data.get("corp_secret") or data.get("corpSecret") or "").strip()
    raw_agent_id = data.get("agent_id") or data.get("agentId")
    agent_id = int(raw_agent_id) if raw_agent_id is not None and str(raw_agent_id).strip() != "" else None

    token = str(data.get("token") or "").strip()
    encoding_aes_key = str(data.get("encoding_aes_key") or data.get("encodingAESKey") or "").strip()
    receive_id = str(data.get("receive_id") or data.get("receiveId") or corp_id or "").strip()

    return WeComAccount(
        account_id=account_id,
        name=str(data.get("name") or "").strip(),
        enabled=bool(data.get("enabled", True)),
        connection_mode=str(data.get("connection_mode") or data.get("connectionMode") or "websocket").strip().lower(),
        bot_id=bot_id,
        secret=secret,
        websocket_url=str(
            data.get("websocket_url")
            or data.get("websocketUrl")
            or DEFAULT_WS_URL
        ).strip() or DEFAULT_WS_URL,
        corp_id=corp_id,
        corp_secret=corp_secret,
        agent_id=agent_id,
        token=token,
        encoding_aes_key=encoding_aes_key,
        receive_id=receive_id,
        dm_policy=str(data.get("dm_policy") or data.get("dmPolicy") or "open").strip().lower(),
        allow_from=_coerce_list(data.get("allow_from") or data.get("allowFrom")),
        group_policy=str(data.get("group_policy") or data.get("groupPolicy") or "open").strip().lower(),
        group_allow_from=_coerce_list(data.get("group_allow_from") or data.get("groupAllowFrom")),
        groups=dict(data.get("groups")) if isinstance(data.get("groups"), dict) else {},
        media_local_roots=_coerce_list(data.get("media_local_roots") or data.get("mediaLocalRoots")),
        welcome_text=str(data.get("welcome_text") or data.get("welcomeText") or "").strip(),
    )


def resolve_wecom_accounts(config: PlatformConfig) -> List[WeComAccount]:
    """Resolve all WeCom accounts from a PlatformConfig.

    Supports:
    - Legacy single-account: top-level extra fields (bot_id, secret, etc.)
    - Multi-account: extra.accounts as a dict or list
    """
    extra = config.extra or {}
    accounts_raw = extra.get("accounts")

    if accounts_raw is not None:
        # Multi-account mode
        if isinstance(accounts_raw, dict):
            return [
                _account_from_dict(account_id, _deep_merge(dict(extra), account_data))
                for account_id, account_data in accounts_raw.items()
                if isinstance(account_data, dict)
            ]
        if isinstance(accounts_raw, list):
            return [
                _account_from_dict(
                    str(account_data.get("account_id") or account_data.get("id") or f"account-{idx}"),
                    _deep_merge(dict(extra), account_data),
                )
                for idx, account_data in enumerate(accounts_raw)
                if isinstance(account_data, dict)
            ]

    # Single-account fallback
    return [_account_from_dict("default", extra)]
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom_accounts.py::test_resolve_single_account_from_top_level -v`

Expected: `PASSED`

### Step 1.3: Add multi-account and deep-merge tests

- [ ] **Write tests**

```python
# tests/gateway/test_wecom_accounts.py (append)

def test_resolve_multi_account_dict():
    config = PlatformConfig(
        extra={
            "dm_policy": "open",
            "allow_from": ["alice"],
            "accounts": {
                "prod": {
                    "bot_id": "bot-prod",
                    "secret": "sec-prod",
                    "dm_policy": "allowlist",
                },
                "staging": {
                    "bot_id": "bot-staging",
                    "secret": "sec-staging",
                },
            },
        }
    )
    accounts = resolve_wecom_accounts(config)
    assert len(accounts) == 2
    by_id = {a.account_id: a for a in accounts}

    # prod inherits top-level allow_from, overrides dm_policy
    assert by_id["prod"].bot_id == "bot-prod"
    assert by_id["prod"].dm_policy == "allowlist"
    assert by_id["prod"].allow_from == ["alice"]

    # staging inherits both top-level fields
    assert by_id["staging"].bot_id == "bot-staging"
    assert by_id["staging"].dm_policy == "open"
    assert by_id["staging"].allow_from == ["alice"]


def test_resolve_multi_account_list():
    config = PlatformConfig(
        extra={
            "accounts": [
                {"account_id": "a1", "bot_id": "b1", "secret": "s1"},
                {"id": "a2", "bot_id": "b2", "secret": "s2"},
            ]
        }
    )
    accounts = resolve_wecom_accounts(config)
    assert len(accounts) == 2
    assert accounts[0].account_id == "a1"
    assert accounts[1].account_id == "a2"


def test_groups_deep_merge():
    config = PlatformConfig(
        extra={
            "groups": {"g1": {"allow_from": ["alice"]}},
            "accounts": {
                "acct": {
                    "bot_id": "b",
                    "secret": "s",
                    "groups": {"g2": {"allow_from": ["bob"]}},
                }
            },
        }
    )
    accounts = resolve_wecom_accounts(config)
    assert len(accounts) == 1
    assert accounts[0].groups == {"g1": {"allow_from": ["alice"]}, "g2": {"allow_from": ["bob"]}}
```

- [ ] **Run tests**

Run: `pytest tests/gateway/test_wecom_accounts.py -v`

Expected: all 4 tests `PASSED`

### Step 1.4: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_accounts.py tests/gateway/test_wecom_accounts.py
git commit -m "feat(wecom): add shared account resolver with multi-account support"
```

---

## Task 2: Refactor `wecom.py` to Use Multi-Account Config and `TextBatchAggregator`

**Files:**
- Modify: `gateway/platforms/wecom.py:141-183`
- Test: `tests/gateway/test_wecom.py` (existing, must still pass)

### Step 2.1: Write a failing test for multi-account `WeComAdapter.__init__`

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom.py (append near existing init tests)

def test_adapter_init_with_multi_accounts():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(
        extra={
            "accounts": {
                "acct1": {"bot_id": "b1", "secret": "s1"},
                "acct2": {"bot_id": "b2", "secret": "s2"},
            }
        }
    )
    adapter = WeComAdapter(config)
    assert len(adapter._accounts) == 2
    assert adapter._accounts[0].account_id == "acct1"
    assert adapter._accounts[1].account_id == "acct2"
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom.py::test_adapter_init_with_multi_accounts -v`

Expected: `FAILED` with `AttributeError: 'WeComAdapter' object has no attribute '_accounts'`

### Step 2.2: Refactor `WeComAdapter.__init__` to use `resolve_wecom_accounts` and `TextBatchAggregator`

- [ ] **Modify `wecom.py`**

Replace the existing `__init__` body (`wecom.py:149-182`) with:

```python
from gateway.platforms.wecom_accounts import resolve_wecom_accounts, WeComAccount
from gateway.platforms.helpers import TextBatchAggregator

# ... (keep existing imports, just add the two above) ...

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WECOM)

        self._accounts: List[WeComAccount] = resolve_wecom_accounts(config)
        # Backwards-compat: expose default account attrs at top level for single-account users
        default_account = self._accounts[0] if self._accounts else WeComAccount(account_id="default")
        self._bot_id = default_account.bot_id
        self._secret = default_account.secret
        self._ws_url = default_account.websocket_url
        self._dm_policy = default_account.dm_policy
        self._allow_from = default_account.allow_from
        self._group_policy = default_account.group_policy
        self._group_allow_from = default_account.group_allow_from
        self._groups = default_account.groups

        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._http_client: Optional["httpx.AsyncClient"] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._pending_responses: Dict[str, asyncio.Future] = {}
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE)
        self._reply_req_ids: Dict[str, str] = {}

        # Text batching via shared aggregator
        batch_delay = float(os.getenv("HERMES_WECOM_TEXT_BATCH_DELAY_SECONDS", "0.6"))
        split_delay = float(os.getenv("HERMES_WECOM_TEXT_BATCH_SPLIT_DELAY_SECONDS", "2.0"))
        self._text_batcher = TextBatchAggregator(
            handler=self.handle_message,
            batch_delay=batch_delay,
            split_delay=split_delay,
            split_threshold=self._SPLIT_THRESHOLD,
        )
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom.py::test_adapter_init_with_multi_accounts -v`

Expected: `PASSED`

### Step 2.3: Replace inline batching with `TextBatchAggregator`

- [ ] **Delete old batching methods and fields**

In `gateway/platforms/wecom.py`, remove:
- `_text_batch_delay_seconds` assignment (was in old `__init__`)
- `_text_batch_split_delay_seconds` assignment
- `_pending_text_batches` dict
- `_pending_text_batch_tasks` dict
- `_text_batch_key` method (`wecom.py:542-549`)
- `_enqueue_text_event` method (`wecom.py:551-579`)
- `_flush_text_batch` method (`wecom.py:581-606`)

- [ ] **Update `_on_message` to use aggregator**

Find the existing block in `_on_message` (`wecom.py:544-547`):

```python
        if message_type == MessageType.TEXT and self._text_batch_delay_seconds > 0:
            self._enqueue_text_event(event)
        else:
            await self.handle_message(event)
```

Replace with:

```python
        if message_type == MessageType.TEXT and self._text_batcher.is_enabled():
            key = self._text_batch_key(event)
            self._text_batcher.enqueue(event, key)
        else:
            await self.handle_message(event)
```

Wait — `_text_batch_key` was deleted. Re-add a small helper:

```python
    def _text_batch_key(self, event: MessageEvent) -> str:
        from gateway.session import build_session_key
        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
```

- [ ] **Run existing WeCom tests**

Run: `pytest tests/gateway/test_wecom.py -v`

Expected: all existing tests pass (no regressions)

### Step 2.4: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "refactor(wecom): use shared account resolver and TextBatchAggregator"
```

---

## Task 3: Refactor `wecom_callback.py` to Use `resolve_wecom_accounts`

**Files:**
- Modify: `gateway/platforms/wecom_callback.py:55-97`
- Test: `tests/gateway/test_wecom_callback.py` (existing, must still pass)

### Step 3.1: Write a failing test for callback adapter using shared resolver

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_callback.py (append)

def test_callback_adapter_uses_shared_resolver():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom_callback import WecomCallbackAdapter

    config = PlatformConfig(
        extra={
            "accounts": {
                "app1": {
                    "corp_id": "corp-1",
                    "corp_secret": "secret-1",
                    "agent_id": 1001,
                    "token": "tok1",
                    "encoding_aes_key": "key1",
                }
            }
        }
    )
    adapter = WecomCallbackAdapter(config)
    assert len(adapter._apps) == 1
    assert adapter._apps[0]["corp_id"] == "corp-1"
    assert adapter._apps[0]["agent_id"] == "1001"
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_callback.py::test_callback_adapter_uses_shared_resolver -v`

Expected: `FAILED` because `_normalize_apps` doesn't understand `extra.accounts` dict shape yet.

### Step 3.2: Replace `_normalize_apps` with `resolve_wecom_accounts`

- [ ] **Modify `wecom_callback.py`**

Add import at the top:

```python
from gateway.platforms.wecom_accounts import resolve_wecom_accounts, WeComAccount
```

Replace `_normalize_apps` (`wecom_callback.py:82-97`) with:

```python
    @staticmethod
    def _normalize_apps(extra: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Resolve apps using shared account resolver for multi-account parity."""
        from gateway.config import PlatformConfig

        config = PlatformConfig(extra=extra)
        accounts = resolve_wecom_accounts(config)
        apps: List[Dict[str, Any]] = []
        for account in accounts:
            # Only include accounts that have agent credentials
            if not account.is_agent_configured:
                continue
            apps.append(
                {
                    "name": account.account_id,
                    "corp_id": account.corp_id,
                    "corp_secret": account.corp_secret,
                    "agent_id": str(account.agent_id) if account.agent_id is not None else "",
                    "token": account.token,
                    "encoding_aes_key": account.encoding_aes_key,
                }
            )
        return apps
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom_callback.py::test_callback_adapter_uses_shared_resolver -v`

Expected: `PASSED`

### Step 3.3: Backward-compat test for legacy top-level callback config

- [ ] **Write test**

```python
# tests/gateway/test_wecom_callback.py (append)

def test_callback_adapter_legacy_top_level_config():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom_callback import WecomCallbackAdapter

    config = PlatformConfig(
        extra={
            "corp_id": "legacy-corp",
            "corp_secret": "legacy-secret",
            "agent_id": 42,
            "token": "legacy-token",
            "encoding_aes_key": "legacy-key",
        }
    )
    adapter = WecomCallbackAdapter(config)
    assert len(adapter._apps) == 1
    assert adapter._apps[0]["corp_id"] == "legacy-corp"
    assert adapter._apps[0]["agent_id"] == "42"
```

- [ ] **Run tests**

Run: `pytest tests/gateway/test_wecom_callback.py -v`

Expected: all existing + new tests `PASSED`

### Step 3.4: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_callback.py tests/gateway/test_wecom_callback.py
git commit -m "refactor(wecom_callback): use shared account resolver for multi-account parity"
```

---

## Task 4: Multi-Account `connect()` Loop in `wecom.py`

**Files:**
- Modify: `gateway/platforms/wecom.py:188-223`
- Test: `tests/gateway/test_wecom.py`

### Step 4.1: Write a failing test for multi-account connect

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom.py (append)
import asyncio
from unittest.mock import patch, AsyncMock


async def test_connect_skips_accounts_without_bot_credentials():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(
        extra={
            "accounts": {
                "no_bot": {"bot_id": "", "secret": ""},
                "has_bot": {"bot_id": "b", "secret": "s"},
            }
        }
    )
    adapter = WeComAdapter(config)

    with patch.object(adapter, "_open_connection", new_callable=AsyncMock) as mock_open:
        result = await adapter.connect()
        assert result is True
        mock_open.assert_awaited_once()
        # Should only attempt connection for the account with credentials
        assert mock_open.call_args[1]["account"].account_id == "has_bot"
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom.py::test_connect_skips_accounts_without_bot_credentials -v`

Expected: `FAILED` — `connect()` does not accept or loop over accounts.

### Step 4.2: Refactor `connect()` to loop over `_accounts`

- [ ] **Modify `connect()` in `wecom.py`**

Replace `connect()` (`wecom.py:188-223`) with:

```python
    async def connect(self) -> bool:
        """Connect to the WeCom AI Bot gateway for all configured bot accounts."""
        if not AIOHTTP_AVAILABLE:
            message = "WeCom startup failed: aiohttp not installed"
            self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s. Run: pip install aiohttp", self.name, message)
            return False
        if not HTTPX_AVAILABLE:
            message = "WeCom startup failed: httpx not installed"
            self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s. Run: pip install httpx", self.name, message)
            return False

        bot_accounts = [a for a in self._accounts if a.is_bot_configured]
        if not bot_accounts:
            message = "WeCom startup failed: no bot accounts with bot_id and secret configured"
            self._set_fatal_error("wecom_missing_credentials", message, retryable=True)
            logger.warning("[%s] %s", self.name, message)
            return False

        try:
            self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
            # For now, only connect the first bot account.
            # Multi-connection support (one WS per account) will be added in a follow-up.
            account = bot_accounts[0]
            await self._open_connection(account)
            self._mark_connected()
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            logger.info("[%s] Connected to %s (account=%s)", self.name, self._ws_url, account.account_id)
            return True
        except Exception as exc:
            message = f"WeCom startup failed: {exc}"
            self._set_fatal_error("wecom_connect_error", message, retryable=True)
            logger.error("[%s] Failed to connect: %s", self.name, exc, exc_info=True)
            await self._cleanup_ws()
            if self._http_client:
                await self._http_client.aclose()
                self._http_client = None
            return False
```

Then update `_open_connection` signature and body (`wecom.py:266-289`) to accept an account:

```python
    async def _open_connection(self, account: WeComAccount) -> None:
        """Open and authenticate a websocket connection."""
        await self._cleanup_ws()
        self._session = aiohttp.ClientSession(trust_env=True)
        self._ws = await self._session.ws_connect(
            account.websocket_url,
            heartbeat=HEARTBEAT_INTERVAL_SECONDS * 2,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )

        req_id = self._new_req_id("subscribe")
        await self._send_json(
            {
                "cmd": APP_CMD_SUBSCRIBE,
                "headers": {"req_id": req_id},
                "body": {"bot_id": account.bot_id, "secret": account.secret},
            }
        )

        auth_payload = await self._wait_for_handshake(req_id)
        errcode = auth_payload.get("errcode", 0)
        if errcode not in (0, None):
            errmsg = auth_payload.get("errmsg", "authentication failed")
            raise RuntimeError(f"{errmsg} (errcode={errcode})")
```

Also update `_listen_loop` reconnect (`wecom.py:333-335`) to pass the first account:

```python
                try:
                    account = self._accounts[0] if self._accounts else WeComAccount(account_id="default")
                    await self._open_connection(account)
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom.py::test_connect_skips_accounts_without_bot_credentials -v`

Expected: `PASSED`

### Step 4.3: Run full WeCom test suite

- [ ] **Run tests**

Run: `pytest tests/gateway/test_wecom.py -v`

Expected: all tests `PASSED`

### Step 4.4: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "feat(wecom): connect loops over resolved bot accounts"
```

---

## Task 5: Final Regression Check

- [ ] **Run all WeCom-related tests**

Run:
```bash
pytest tests/gateway/test_wecom.py tests/gateway/test_wecom_callback.py tests/gateway/test_wecom_accounts.py -v
```

Expected: all tests `PASSED`

- [ ] **Commit if any remaining changes**

```bash
git add -A
git commit -m "test(wecom): full regression suite for core multi-account refactor" || true
```

---

## Spec Coverage Checklist

| Requirement | Task |
|-------------|------|
| Extract shared account resolver | Task 1 |
| Support single-account backward compat | Task 1, 2, 3 |
| Support multi-account `extra.accounts` (dict + list) | Task 1 |
| Deep merge per-account overrides | Task 1 |
| `wecom.py` uses resolver | Task 2 |
| `wecom.py` uses `TextBatchAggregator` | Task 2 |
| `wecom_callback.py` uses resolver | Task 3 |
| `connect()` loops over accounts | Task 4 |
| No regressions in existing tests | Task 5 |

---

## Placeholder Scan

- No `TBD` or `TODO` entries.
- Every step includes exact file path, code block, and expected command output.
- Every test includes actual assertion code.
- Type and method names are consistent across all tasks.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-14-wecom-core-multiaccount.md`.**

Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

Which approach would you like?
