from gateway.platforms.wecom.command_auth import is_command, resolve_command_auth, build_unauthorized_command_prompt
from gateway.platforms.wecom.accounts import WeComAccount


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


class TestCommandAuthGroups:
    def test_group_allowlist_blocks_unauthorized_group(self):
        account = WeComAccount(
            account_id="a1",
            group_policy="allowlist",
            group_allow_from=["group-1"],
        )
        result = resolve_command_auth(account, "/new", "user-1", chat_id="group-2", chat_type="group")
        assert result.command_authorized is False

    def test_group_allowlist_allows_authorized_group(self):
        account = WeComAccount(
            account_id="a1",
            group_policy="allowlist",
            group_allow_from=["group-1"],
        )
        result = resolve_command_auth(account, "/new", "user-1", chat_id="group-1", chat_type="group")
        assert result.command_authorized is True

    def test_group_disabled_blocks_all_commands(self):
        account = WeComAccount(
            account_id="a1",
            group_policy="disabled",
        )
        result = resolve_command_auth(account, "/new", "user-1", chat_id="group-1", chat_type="group")
        assert result.command_authorized is False

    def test_group_open_allows_commands(self):
        account = WeComAccount(
            account_id="a1",
            group_policy="open",
        )
        result = resolve_command_auth(account, "/new", "user-1", chat_id="group-1", chat_type="group")
        assert result.command_authorized is True

    def test_per_command_allowlist_restricts_specific_commands(self):
        account = WeComAccount(
            account_id="a1",
            groups={
                "commands": {
                    "/new": {"allow_from": ["admin-1"]},
                    "/reset": {"allow_from": ["admin-1"]},
                }
            },
        )
        result = resolve_command_auth(account, "/new", "user-1")
        assert result.command_authorized is False

        result = resolve_command_auth(account, "/new", "admin-1")
        assert result.command_authorized is True

    def test_per_command_allowlist_does_not_affect_other_commands(self):
        account = WeComAccount(
            account_id="a1",
            groups={
                "commands": {
                    "/new": {"allow_from": ["admin-1"]},
                }
            },
        )
        result = resolve_command_auth(account, "/reset", "user-1")
        assert result.command_authorized is True

    def test_non_command_text_is_always_authorized(self):
        account = WeComAccount(account_id="a1", dm_policy="allowlist", allow_from=["user-1"])
        result = resolve_command_auth(account, "Hello", "user-2")
        assert result.command_authorized is True

    def test_group_check_only_applies_to_group_chats(self):
        account = WeComAccount(
            account_id="a1",
            dm_policy="allowlist",
            allow_from=["user-1"],
            group_policy="allowlist",
            group_allow_from=["group-1"],
        )
        # DM chat — group policy should not apply
        result = resolve_command_auth(account, "/new", "user-2", chat_id="user-2", chat_type="")
        assert result.command_authorized is False  # blocked by DM allowlist

    def test_dm_allowlist_still_works_with_group_params(self):
        account = WeComAccount(
            account_id="a1",
            dm_policy="allowlist",
            allow_from=["user-1"],
        )
        result = resolve_command_auth(account, "/new", "user-1", chat_id="user-1", chat_type="")
        assert result.command_authorized is True
