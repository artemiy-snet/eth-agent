"""Мінімальний клієнт Claude Messages API на urllib (без SDK — працює в Termux)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

API = "https://api.anthropic.com/v1/messages"


class ClaudeError(Exception):
    pass


def headers(api_key: str, workspace_id: str = "") -> dict:
    """Ключ організації (multi-workspace) вимагає anthropic-workspace-id; ключ одного простору — ні."""
    h = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    if workspace_id:
        h["anthropic-workspace-id"] = workspace_id
    return h


def explain_http(code: int, body: str) -> str:
    """Людське пояснення помилки API (повідомлення сервера + підказка)."""
    try:
        msg = json.loads(body).get("error", {}).get("message", "")
    except ValueError:
        msg = body[:200]
    hint = ""
    if code == 400 and "workspace" in msg.lower():
        hint = " → ключ рівня «Організація»: додай ANTHROPIC_WORKSPACE_ID або створи ключ для простору Default"
    elif code == 401:
        hint = " → ключ недійсний або скопійований не повністю"
    elif code == 403:
        hint = " → у ключа немає доступу (перевір простір/права)"
    elif code == 402 or "credit" in msg.lower():
        hint = " → закінчились кредити"
    return f"HTTP {code}: {msg[:200]}{hint}"


def complete(api_key: str, model: str, system: str, user: str, max_tokens: int = 1500,
             timeout: float = 90, workspace_id: str = "") -> str:
    body = json.dumps({"model": model, "max_tokens": max_tokens, "system": system,
                       "messages": [{"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers=headers(api_key, workspace_id))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise ClaudeError(explain_http(e.code, e.read().decode(errors="replace"))) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ClaudeError(f"мережа: {e}") from None
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def extract_json(text: str) -> dict:
    """Перший JSON-об'єкт у відповіді (модель інколи обгортає його в ```json)."""
    start = text.find("{")
    if start < 0:
        raise ValueError("у відповіді немає JSON")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("JSON не закрито")
