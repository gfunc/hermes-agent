from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from gateway.platforms.wecom.accounts import WeComAccount


def is_command(raw_body: str) -> bool:
    text = str(raw_body or "").strip()
    if not text or text.startswith("http://") or text.startswith("https://"):
        return False
    return text.startswith("/") and len(text) > 1


@dataclass
class CommandAuthResult:
    command_authorized: bool
    should_compute_auth: bool
    dm_policy: str
    sender_allowed: bool
    authorizer_configured: bool


def _entry_matches(entries: List[str], target: str) -> bool:
    normalized_target = str(target).strip().lower()
    for entry in entries:
        normalized = str(entry).strip().lower()
        if normalized == "*" or normalized == normalized_target:
            return True
    return False


def resolve_command_auth(
    account: WeComAccount,
    raw_body: str,
    sender_user_id: str,
) -> CommandAuthResult:
    dm_policy = account.dm_policy
    allow_from = account.allow_from
    sender_allowed = _entry_matches(allow_from, sender_user_id)

    # authorizer_configured: check if an authorizer is set in account config
    authorizer_configured = bool(
        isinstance(account.groups, dict) and account.groups.get("authorizer")
    )

    should_compute_auth = is_command(raw_body) and dm_policy == "allowlist"

    if not should_compute_auth:
        return CommandAuthResult(
            command_authorized=True,
            should_compute_auth=False,
            dm_policy=dm_policy,
            sender_allowed=sender_allowed,
            authorizer_configured=authorizer_configured,
        )

    command_authorized = sender_allowed
    return CommandAuthResult(
        command_authorized=command_authorized,
        should_compute_auth=True,
        dm_policy=dm_policy,
        sender_allowed=sender_allowed,
        authorizer_configured=authorizer_configured,
    )


def build_unauthorized_command_prompt(
    sender_user_id: str,
    dm_policy: str,
    scope: str = "bot",
) -> str:
    return (
        f"抱歉，您没有权限执行该命令。"
        f"当前私信策略为 {dm_policy}，请联系管理员添加到允许列表后重试。"
    )
