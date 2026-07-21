"""召回衰减: 软加权 + 硬阈值。"""
from __future__ import annotations


class RecallWeighter:
    def __init__(self, *, staleness_floor: float = 0.7,
                 staleness_soft: float = 0.5,
                 weight_floor: float = 0.5):
        self.staleness_floor = staleness_floor
        self.staleness_soft = staleness_soft
        self.weight_floor = weight_floor

    def apply(self, results: list) -> list:
        """results: [(Memory, score), ...] → 软加权后重排, 硬阈值过滤。"""
        out = []
        for mem, score in results:
            staleness = getattr(mem, "staleness", 0.0) or 0.0
            if staleness >= self.staleness_floor:
                continue
            weight = self._weight(staleness)
            out.append((mem, score * weight))
        out.sort(key=lambda x: -x[1])
        return out

    def _weight(self, staleness: float) -> float:
        if staleness <= self.staleness_soft:
            return 1.0
        ratio = (staleness - self.staleness_soft) / max(1e-6, self.staleness_floor - self.staleness_soft)
        return max(self.weight_floor, 1.0 - ratio * (1.0 - self.weight_floor))
