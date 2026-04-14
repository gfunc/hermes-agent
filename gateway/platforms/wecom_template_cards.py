from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TEMPLATE_CARD_CACHE_TTL_SECONDS = 86400
TEMPLATE_CARD_CACHE_MAX_SIZE = 300

VALID_CARD_TYPES = {
    "text_notice",
    "news_notice",
    "button_interaction",
    "vote_interaction",
    "multiple_interaction",
}

_sent_template_cards: Dict[str, Dict[str, Any]] = {}
_sent_timestamps: Dict[str, float] = {}


def _cache_key(account_id: str, task_id: str) -> str:
    return f"{account_id}:{task_id}"


def _prune_cache() -> None:
    cutoff = time.time() - TEMPLATE_CARD_CACHE_TTL_SECONDS
    expired = [k for k, ts in _sent_timestamps.items() if ts < cutoff]
    for k in expired:
        _sent_template_cards.pop(k, None)
        _sent_timestamps.pop(k, None)
    if len(_sent_timestamps) > TEMPLATE_CARD_CACHE_MAX_SIZE:
        sorted_keys = sorted(_sent_timestamps, key=lambda k: _sent_timestamps[k])
        for k in sorted_keys[: len(sorted_keys) - TEMPLATE_CARD_CACHE_MAX_SIZE]:
            _sent_template_cards.pop(k, None)
            _sent_timestamps.pop(k, None)


def save_template_card_to_cache(account_id: str, card: Dict[str, Any]) -> None:
    task_id = str(card.get("task_id") or "").strip()
    if not task_id:
        return
    key = _cache_key(account_id, task_id)
    _sent_template_cards[key] = dict(card)
    _sent_timestamps[key] = time.time()
    _prune_cache()


def get_template_card_from_cache(account_id: str, task_id: str) -> Optional[Dict[str, Any]]:
    key = _cache_key(account_id, task_id)
    ts = _sent_timestamps.get(key)
    if ts is None or time.time() - ts > TEMPLATE_CARD_CACHE_TTL_SECONDS:
        _sent_template_cards.pop(key, None)
        _sent_timestamps.pop(key, None)
        return None
    return dict(_sent_template_cards.get(key) or {})


def _coerce_checkbox_mode(value: Any) -> Optional[int]:
    aliases = {"single": 0, "radio": 0, "单选": 0, "multi": 1, "multiple": 1, "多选": 1}
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in aliases:
            return aliases[lowered]
        try:
            num = int(lowered)
            return 0 if num <= 0 else 1
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return 0 if int(value) <= 0 else 1
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return None


def _normalize_card(card: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(card)
    # Validate card_type
    card_type = str(normalized.get("card_type") or "").strip()
    if card_type not in VALID_CARD_TYPES:
        logger.warning("[wecom][template-card] Unknown card_type: %s", card_type)
    # Ensure task_id exists
    if not str(normalized.get("task_id") or "").strip():
        normalized["task_id"] = f"task-{int(time.time() * 1000)}"
    # checkbox.mode normalization
    checkbox = normalized.get("checkbox")
    if isinstance(checkbox, dict):
        mode = _coerce_checkbox_mode(checkbox.get("mode"))
        if mode is not None:
            checkbox["mode"] = mode
        disable = _coerce_bool(checkbox.get("disable"))
        if disable is not None:
            checkbox["disable"] = disable
        options = checkbox.get("option_list")
        if isinstance(options, list):
            for opt in options:
                if isinstance(opt, dict):
                    checked = _coerce_bool(opt.get("is_checked"))
                    if checked is not None:
                        opt["is_checked"] = checked
    # Common integer fields
    for path in [
        ("source", "desc_color"),
        ("quote_area", "type"),
        ("card_action", "type"),
        ("image_text_area", "type"),
    ]:
        parent_key, child_key = path
        parent = normalized.get(parent_key)
        if isinstance(parent, dict):
            val = _coerce_int(parent.get(child_key))
            if val is not None:
                parent[child_key] = val
    # List-level integer fields
    for list_key, item_key in [
        ("horizontal_content_list", "type"),
        ("jump_list", "type"),
        ("button_list", "style"),
        ("select_list", "disable"),
    ]:
        items = normalized.get(list_key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    if item_key == "disable":
                        val = _coerce_bool(item.get(item_key))
                    else:
                        val = _coerce_int(item.get(item_key))
                    if val is not None:
                        item[item_key] = val
    # button_selection.disable
    btn_sel = normalized.get("button_selection")
    if isinstance(btn_sel, dict):
        val = _coerce_bool(btn_sel.get("disable"))
        if val is not None:
            btn_sel["disable"] = val
    return normalized


@dataclass
class TemplateCardResult:
    cards: List[Dict[str, Any]]
    remaining_text: str


def extract_template_cards(text: str) -> TemplateCardResult:
    pattern = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
    cards: List[Dict[str, Any]] = []
    remaining = str(text or "")
    for match in pattern.finditer(text):
        block = match.group(1).strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and str(parsed.get("card_type") or "").strip():
                cards.append(_normalize_card(parsed))
                remaining = remaining.replace(match.group(0), "")
        except json.JSONDecodeError:
            continue
    return TemplateCardResult(cards=cards, remaining_text=remaining.strip())


def mask_template_card_blocks(text: str) -> str:
    pattern = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)

    def replacer(match: "re.Match[str]") -> str:
        block = match.group(1).strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and str(parsed.get("card_type") or "").strip():
                return ""
        except json.JSONDecodeError:
            pass
        return match.group(0)

    return pattern.sub(replacer, text).strip()
