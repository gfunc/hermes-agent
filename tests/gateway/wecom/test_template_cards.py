from gateway.platforms.wecom.template_cards import extract_template_cards, mask_template_card_blocks


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
