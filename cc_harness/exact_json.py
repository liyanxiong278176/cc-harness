"""Literal JSON delivery requirements derived from public task instructions."""
from __future__ import annotations

import ast
import json
import re


def canonical_json(text: str) -> str:
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    value = json.loads(text, object_pairs_hook=unique_object)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


_DIRECTIVE = re.compile(
    r"\b(?:write|save|create|produce)\s+(?:the\s+file\s+)?`?"
    r"(?P<path>[\w./\\-]+\.json)`?\s+(?:exactly\s+)?as\s+(?:exactly\s+)?`?\s*(?P<literal>[{\[])",
    re.I,
)


def _literal_prefix(text: str) -> str | None:
    stack = []
    quote = None
    escaped = False
    for index, char in enumerate(text[:4096]):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "{[":
            stack.append(char)
            if len(stack) > 32:
                return None
        elif char in "}]":
            if not stack or stack.pop() != {"}": "{", "]": "["}[char]:
                return None
            if not stack:
                return text[:index + 1]
    return None


class _JsonNames(ast.NodeTransformer):
    def visit_Name(self, node):
        values = {"true": True, "false": False, "null": None}
        return ast.Constant(values[node.id]) if node.id in values else node


def literal_json_artifacts(instruction: str) -> dict[str, str]:
    """Extract concrete write requests, ignoring examples and open schemas.

    No code is executed. Python-style quoted keys and JSON boolean/null
    literals are accepted; expressions and schema placeholders are not.
    """
    result = {}
    conflicts = set()
    for match in _DIRECTIVE.finditer(instruction):
        prefix = re.split(r"[\n.!?;]", instruction[:match.start()])[-1]
        if re.search(r"\b(?:do not|don't|never|example|for instance)\b", prefix, re.I):
            continue
        literal = _literal_prefix(instruction[match.start("literal"):])
        if literal is None:
            continue
        suffix = instruction[match.start("literal") + len(literal):].split("\n", 1)[0]
        if re.search(r"\b(?:allow|include|with|permit)\s+(?:additional|extra|other)\s+(?:fields|keys)|at least", suffix, re.I):
            continue
        try:
            tree = _JsonNames().visit(ast.parse(literal, mode="eval"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    keys = [ast.literal_eval(key) for key in node.keys]
                    if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
                        raise ValueError("JSON keys must be unique strings")
            value = ast.literal_eval(tree)
            encoded = canonical_json(json.dumps(value, allow_nan=False))
        except (SyntaxError, ValueError, TypeError, RecursionError):
            continue
        path = match["path"].replace("\\", "/")
        if path in result and result[path] != encoded:
            conflicts.add(path)
        result[path] = encoded
    return {path: value for path, value in result.items() if path not in conflicts}
