"""Extracting inline JSON blobs from server-rendered pages."""

from __future__ import annotations

import json


def extract_window_json(html: str, variable: str) -> dict | None:
    """Pull `window.<variable> = {...}` out of an inline script.

    Brace-matched rather than regex-matched: these blobs run to megabytes and
    contain every bracket and quote character inside string values, which a
    non-greedy regex either truncates or fails on outright.
    """
    marker = f"window.{variable}"
    marker_at = html.find(marker)
    if marker_at == -1:
        return None

    start = html.find("{", marker_at)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(html)):
        char = html[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : index + 1])
                except (json.JSONDecodeError, ValueError):
                    return None

    return None
