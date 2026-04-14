from gateway.platforms.wecom_command_auth import is_command, resolve_command_auth, build_unauthorized_command_prompt
from gateway.platforms.wecom_accounts import WeComAccount


def test_is_command_detects_slash_commands():
    assert is_command("/reset") is True
    assert is_command("/new session") is True


def test_is_command_ignores_plain_text_and_urls():
    assert is_command("hello world") is False
    assert is_command("https://example.com") is False
    assert is_command("/") is False


def test_open_policy_allows_commands():
    account = WeComAccount(account_id="a1", dm_policy="open")
    result = resolve_command_auth(account, "/reset", "user1")
    assert result.command_authorized is True
    assert result.should_compute_auth is False


def test_allowlist_blocks_unknown_sender():
    account = WeComAccount(account_id="a1", dm_policy="allowlist", allow_from=["alice"])
    result = resolve_command_auth(account, "/reset", "bob")
    assert result.should_compute_auth is True
    assert result.command_authorized is False


def test_allowlist_allows_known_sender():
    account = WeComAccount(account_id="a1", dm_policy="allowlist", allow_from=["alice"])
    result = resolve_command_auth(account, "/reset", "alice")
    assert result.command_authorized is True


def test_wildcard_allowlist_allows_anyone():
    account = WeComAccount(account_id="a1", dm_policy="allowlist", allow_from=["*"])
    result = resolve_command_auth(account, "/reset", "anyone")
    assert result.command_authorized is True
