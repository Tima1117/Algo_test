"""Автоматический реестр адаптеров УК: любой модуль пакета с наследником Adapter."""
from __future__ import annotations

import importlib
import pkgutil

from .base import Adapter, Context


def load_adapters() -> list[Adapter]:
    adapters: list[Adapter] = []
    for mod in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if mod.name.startswith("_") or mod.name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{mod.name}")
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, Adapter) and obj is not Adapter and obj.uk_inns:
                adapters.append(obj())
    seen: dict[str, str] = {}
    for a in adapters:
        for inn in a.uk_inns:
            if inn in seen:
                raise RuntimeError(f"ИНН {inn} заявлен в двух адаптерах: {seen[inn]} и {a.key}")
            seen[inn] = a.key
    return adapters


__all__ = ["Adapter", "Context", "load_adapters"]
