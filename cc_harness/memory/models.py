"""L0-L3 分层记忆数据结构。"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Scenario:
    """L2 场景块(同 session L1 聚类)。"""
    atom_ids: list[str]
    summary: str
    session_id: str
    md_path: str


@dataclass
class Persona:
    """L3 用户画像。"""
    summary: str
    scenario_ids: list[str]
    md_path: str


@dataclass
class RecallResult:
    """分层召回结果(高层 Persona/Scenario + 底层 Atom)。"""
    persona: Persona | None = None
    scenarios: list[Scenario] = field(default_factory=list)
    atoms: list = field(default_factory=list)
