"""Ordered, explicit decisions at stable kernel lifecycle boundaries."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping


@dataclass(frozen=True)
class KernelDecision:
    allow: bool = True
    reason: str = ""
    patch: Mapping[str, Any] = field(default_factory=dict)


Hook = Callable[[str, Mapping[str, Any]], Awaitable[KernelDecision]]


class KernelMiddleware:
    def __init__(self) -> None:
        self._hooks: list[tuple[int, str, frozenset[str], Hook]] = []

    def register(self, name: str, hook: Hook, *, order: int = 0,
                 writes: frozenset[str] = frozenset()) -> None:
        if any(entry[1] == name for entry in self._hooks):
            raise ValueError(f"duplicate middleware: {name}")
        self._hooks.append((order, name, writes, hook))
        self._hooks.sort(key=lambda entry: (entry[0], entry[1]))

    async def run(self, point: str, context: Mapping[str, Any]) -> KernelDecision:
        import json
        from types import MappingProxyType
        patch: dict[str, Any] = {}
        for _order, name, writes, hook in self._hooks:
            # A JSON copy isolates nested values as well as the top-level mapping.
            view = MappingProxyType(json.loads(json.dumps({**context, **patch})))
            decision = await hook(point, view)
            if not isinstance(decision, KernelDecision):
                raise TypeError(f"middleware {name} returned an invalid decision")
            if set(decision.patch) - writes:
                raise ValueError(f"middleware {name} wrote outside its declared scope")
            for key, value in decision.patch.items():
                if key in patch and patch[key] != value:
                    raise ValueError(f"conflicting middleware patch: {key}")
                patch[key] = value
            if not decision.allow:
                return KernelDecision(False, decision.reason, patch)
        return KernelDecision(patch=patch)
