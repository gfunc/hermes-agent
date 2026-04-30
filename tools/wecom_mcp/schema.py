"""Schema cleaning for Claude (Anthropic API).

Unlike Gemini, Claude supports most JSON Schema keywords natively.
We only need to inline $ref/$defs references in case the Anthropic SDK
doesn't handle them.
"""

from typing import Any


def clean_schema_for_claude(schema: Any) -> Any:
    """Inline $ref references. Preserve all other keywords (Claude supports them)."""
    if not isinstance(schema, dict):
        return schema

    defs: dict[str, Any] = {}
    if "$defs" in schema and isinstance(schema["$defs"], dict):
        defs.update(schema["$defs"])
    if "definitions" in schema and isinstance(schema["definitions"], dict):
        defs.update(schema["definitions"])

    return _clean_with_defs(schema, defs, set())


def _clean_with_defs(schema: Any, defs: dict[str, Any], visited: set[str]) -> Any:
    if isinstance(schema, list):
        return [_clean_with_defs(item, defs, visited) for item in schema]
    if not isinstance(schema, dict):
        return schema

    obj = dict(schema)

    # Merge any local $defs/definitions into the shared defs map
    if "$defs" in obj and isinstance(obj["$defs"], dict):
        defs = {**defs, **obj["$defs"]}
    if "definitions" in obj and isinstance(obj["definitions"], dict):
        defs = {**defs, **obj["definitions"]}

    # Handle $ref: inline the referenced schema
    if isinstance(obj.get("$ref"), str):
        ref = obj["$ref"]
        if ref in visited:
            return {}
        match = _match_ref(ref)
        if match and match in defs:
            visited = visited | {ref}
            return _clean_with_defs(defs[match], defs, visited)
        return {}

    # Handle const → enum (some older Claude SDKs prefer enum)
    if "const" in obj and "enum" not in obj:
        obj["enum"] = [obj["const"]]

    # Recursively clean all values
    cleaned: dict[str, Any] = {}
    for key, value in obj.items():
        if key in ("$defs", "definitions"):
            continue
        cleaned[key] = _clean_with_defs(value, defs, visited)

    # Filter null-only branches from anyOf/oneOf to reduce noise
    for combo_key in ("anyOf", "oneOf"):
        if combo_key in cleaned:
            branches = cleaned[combo_key]
            if isinstance(branches, list):
                filtered = [
                    b for b in branches
                    if not (isinstance(b, dict) and b.get("type") == "null")
                ]
                if filtered:
                    cleaned[combo_key] = filtered
                else:
                    del cleaned[combo_key]

    return cleaned


def _match_ref(ref: str) -> str | None:
    """Parse '#/$defs/Foo' or '#/definitions/Foo' and return the key."""
    if ref.startswith("#/$defs/"):
        return ref[len("#/$defs/"):]
    if ref.startswith("#/definitions/"):
        return ref[len("#/definitions/"):]
    return None
