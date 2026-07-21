"""E4 Task 5: 矛盾检测 — write-time + maintenance 全库扫。"""
import pytest
from unittest.mock import MagicMock
from cc_harness.memory.maintenance.conflict import ConflictDetector


def make_mem(mid="m1", text="t"):
    m = MagicMock()
    m.id = mid
    m.text = text
    m.created_at = 1.0
    return m


@pytest.mark.asyncio
async def test_check_returns_verdicts():
    llm = MagicMock()
    async def fake_chat(msgs, tools=None):
        for ev_kind, ev_text in [
            ("done", '{"verdicts": [{"other_id": "old1", "verdict": "supersedes", "action": "delete_old"}]}'),
        ]:
            if ev_kind == "done":
                yield MagicMock(kind="done", content=ev_text)
    llm.chat = fake_chat
    det = ConflictDetector(llm)
    new = make_mem("new1", "user uses pnpm")
    similar = [make_mem("old1", "user uses npm")]
    verdicts = await det.check(new, similar)
    assert len(verdicts) == 1
    assert verdicts[0].action == "delete_old"


@pytest.mark.asyncio
async def test_check_llm_failure_returns_empty():
    llm = MagicMock()
    async def boom(*a, **kw):
        raise RuntimeError("api down")
        if False:
            yield
    llm.chat = boom
    det = ConflictDetector(llm)
    verdicts = await det.check(make_mem(), [make_mem("o1")])
    assert verdicts == []


@pytest.mark.asyncio
async def test_check_unrelated_filtered():
    llm = MagicMock()
    async def fake_chat(msgs, tools=None):
        yield MagicMock(kind="done", content='{"verdicts": [{"other_id": "x", "verdict": "unrelated", "action": "noop"}]}')
    llm.chat = fake_chat
    det = ConflictDetector(llm)
    verdicts = await det.check(make_mem("n"), [make_mem("x")])
    assert verdicts == []
