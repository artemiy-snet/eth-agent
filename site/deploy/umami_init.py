#!/usr/bin/env python3
"""Перше налаштування Umami через його API: змінити стандартний пароль admin і створити сайт criptobot.store.

Пароль береться зі змінної середовища UMAMI_NEW_PASS (не з аргументів — щоб його не було видно в списку процесів).
Друкує лише ID сайту (website id) — він потрібен трекеру й /api/stats.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("UMAMI_URL", "http://127.0.0.1:3010").rstrip("/")
DOMAIN = os.environ.get("SITE_DOMAIN", "criptobot.store")


def req(method, path, body=None, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    with urllib.request.urlopen(r, timeout=20) as resp:
        t = resp.read().decode()
        return json.loads(t) if t.strip() else {}


def login(user, pw):
    try:
        return req("POST", "/api/auth/login", {"username": user, "password": pw})["token"]
    except urllib.error.HTTPError:
        return None


def main() -> int:
    new = os.environ.get("UMAMI_NEW_PASS", "")
    if len(new) < 16:
        print("UMAMI_NEW_PASS не задано", file=sys.stderr)
        return 2
    for _ in range(90):                               # чекаємо, поки Umami створить таблиці й підніметься
        try:
            urllib.request.urlopen(URL + "/api/heartbeat", timeout=5).read()
            break
        except Exception:  # noqa: BLE001
            time.sleep(2)
    else:
        print("Umami не відповідає на 127.0.0.1:3010", file=sys.stderr)
        return 1
    tok = login("admin", new)
    if tok is None:
        tok = login("admin", "umami")
        if tok is None:
            print("не вдалося увійти в Umami (ні новим, ні стандартним паролем)", file=sys.stderr)
            return 1
        req("POST", "/api/me/password", {"currentPassword": "umami", "newPassword": new}, tok)
        tok = login("admin", new)
    sites = req("GET", "/api/websites", token=tok)
    rows = sites.get("data", sites) if isinstance(sites, dict) else sites
    site = next((s for s in rows or [] if s.get("domain") == DOMAIN), None)
    if site is None:
        site = req("POST", "/api/websites", {"name": DOMAIN, "domain": DOMAIN}, tok)
    print(site["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
