"""Lenient JSON parsing for LLM output: fence stripping, repair, loads."""
import json
import re

def strip_json(raw: str) -> str:
    """Extract a JSON object/array from a model response.
    Handles: bare JSON, ```json fenced blocks, JSON with trailing summary text after the closing brace.
    """
    import re
    raw = raw.strip()
    # Strip leading code fence if present
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    # If a closing fence exists, cut everything from it onwards
    fence_close = raw.find('\n```')
    if fence_close != -1:
        raw = raw[:fence_close]
    raw = raw.strip()
    # If there's still trailing text after the JSON object, find the matching brace
    # and trim. Walk braces honoring strings.
    if raw and raw[0] in '{[':
        open_ch, close_ch = ('{', '}') if raw[0] == '{' else ('[', ']')
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i, ch in enumerate(raw):
            if in_str:
                if esc:        esc = False
                elif ch == '\\': esc = True
                elif ch == '"': in_str = False
                continue
            if ch == '"': in_str = True
            elif ch == open_ch:  depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end != -1:
            raw = raw[:end]
    return raw.strip()

def _repair_llm_json(s: str) -> str:
    """Best-effort repair of common LLM JSON mistakes: trailing commas,
    unquoted property keys, single-quoted keys/strings."""
    import re
    # Remove trailing commas before } or ]
    s = re.sub(r',(\s*[}\]])', r'\1', s)
    # Quote unquoted object keys: {  foo: ...  -> {"foo": ...
    # Match {/, then whitespace, then identifier followed by :
    s = re.sub(r'([{,])(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*):', r'\1\2"\3"\4:', s)
    # Convert single-quoted keys to double: 'foo': -> "foo":
    s = re.sub(r"([{,])(\s*)'([^'\n]*)'(\s*):", r'\1\2"\3"\4:', s)
    return s

def _strip_markdown_fence(raw: str) -> str:
    """Remove ```json ... ``` (or just ``` ... ```) wrappers that some Claude
    responses bring back even when the system prompt says strict JSON. Cheap
    pre-clean before json.loads."""
    s = (raw or '').strip()
    # Leading ```json\n or ```\n
    s = re.sub(r'^```(?:json|JSON)?[ \t]*\n?', '', s)
    # Trailing ``` (with optional preceding newline)
    s = re.sub(r'\n?```[ \t]*$', '', s)
    return s

def loads_lenient(raw: str):
    """json.loads with markdown-fence strip + repair fallback."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try after stripping ```json fences (Claude sometimes wraps JSON in them
    # despite the prompt asking for strict JSON).
    s = _strip_markdown_fence(raw)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(_repair_llm_json(s))

