import os
from gateway.config import PlatformConfig
from gateway.platforms.wecom.accounts import resolve_wecom_accounts


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
