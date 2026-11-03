"""robots.txt: разбор с подстановками (* и $), как их понимают Яндекс и Google.

Стандартный urllib.robotparser подстановки не поддерживает и пропустил бы правила вроде
«Disallow: /*?» или «Disallow: /*.pdf$», поэтому здесь свой небольшой разборщик.
Правило выбирается по длине совпадения; при равной длине побеждает Allow.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


class Robots:
    def __init__(self, text: str, agent: str):
        self.rules: list[tuple[bool, re.Pattern, int]] = []
        groups: dict[str, list[tuple[bool, str]]] = {}
        agents: list[str] = []
        in_rules = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, value = (x.strip() for x in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if in_rules:
                    agents, in_rules = [], False
                agents.append(value.lower())
                for a in agents:
                    groups.setdefault(a, [])
            elif field in ("allow", "disallow"):
                in_rules = True
                for a in agents:
                    groups.setdefault(a, []).append((field == "allow", value))
        agent = agent.lower()
        chosen = next((rules for name, rules in groups.items() if name != "*" and name in agent), None)
        if chosen is None:
            chosen = groups.get("*", [])
        for allow, path in chosen:
            if not path:
                continue   # пустой Disallow — разрешено всё
            pattern = re.escape(path).replace(r"\*", ".*")
            if pattern.endswith(r"\$"):
                pattern = pattern[:-2] + "$"
            self.rules.append((allow, re.compile(pattern), len(path)))

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: tuple[int, bool] | None = None
        for allow, rx, length in self.rules:
            if rx.match(target) and (best is None or length > best[0] or (length == best[0] and allow)):
                best = (length, allow)
        return True if best is None else best[1]
