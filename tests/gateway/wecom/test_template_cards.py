from gateway.platforms.wecom.template_cards import (
    extract_template_cards,
    mask_template_card_blocks,
    _normalize_card,
)


def test_extract_single_template_card():
    text = "```json\n{\"card_type\":\"text_notice\",\"task_id\":\"t1\"}\n```"
    result = extract_template_cards(text)
    assert len(result.cards) == 1
    assert result.cards[0]["task_id"] == "t1"
    assert result.remaining_text.strip() == ""


def test_mask_template_card_blocks_hides_json():
    text = "hello\n```json\n{\"card_type\":\"text_notice\"}\n```\nworld"
    masked = mask_template_card_blocks(text)
    assert "card_type" not in masked
    assert "hello" in masked
    assert "world" in masked


def test_normalize_vote_interaction_simplified_format():
    """Simplified vote_interaction with options array should map to WeCom format."""
    simplified = {
        "card_type": "vote_interaction",
        "source": {"desc": "Poll"},
        "task_id": "vote-1",
        "options": [
            {"key": "yes", "value": "Yes"},
            {"key": "no", "value": "No"},
        ],
    }
    result = _normalize_card(simplified)
    assert result["card_type"] == "vote_interaction"
    checkbox = result.get("checkbox", {})
    assert "question_key" in checkbox
    assert "option_list" in checkbox
    assert len(checkbox["option_list"]) == 2


def test_normalize_multiple_interaction_simplified_format():
    """Simplified multiple_interaction with buttons array should map to WeCom format."""
    simplified = {
        "card_type": "multiple_interaction",
        "task_id": "btn-1",
        "buttons": [
            {"key": "ok", "value": "OK"},
            {"key": "cancel", "value": "Cancel"},
        ],
    }
    result = _normalize_card(simplified)
    assert result["card_type"] == "multiple_interaction"
    btn_sel = result.get("button_selection", {})
    assert "question_key" in btn_sel
    assert "option_list" in btn_sel
    assert len(btn_sel["option_list"]) == 2


def test_normalize_required_field_completion():
    """Missing required fields should be auto-completed with defaults."""
    card = {
        "card_type": "text_notice",
        # Missing task_id — should be auto-generated
    }
    result = _normalize_card(card)
    assert result.get("task_id", "").startswith("task-")


def test_normalize_string_title_fields_to_object():
    """String emphasis_content / main_title should be normalized to object format."""
    card = {
        "card_type": "text_notice",
        "task_id": "t1",
        "emphasis_content": "Important!",
        "main_title": "Title",
    }
    result = _normalize_card(card)
    assert result["emphasis_content"] == {"title": "Important!"}
    assert result["main_title"] == {"title": "Title"}
