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
