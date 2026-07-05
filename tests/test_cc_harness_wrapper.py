"""Tests for eval/promptfoo/wrappers/cc_harness.py — main.py resolution.

The wrapper computes MAIN_PY by searching upward from its own directory
for a file named main.py. CI historically hit "main.py not found" failures
when the wrapper was invoked from a context where parents[3] didn't contain
main.py. The fallback search walks up to 6 ancestor levels.

These tests verify the search returns the real main.py in the project
layout and returns None when no candidate exists.
"""
import asyncio
import importlib.util
from pathlib import Path

# Load the wrapper directly (it's not on Python path).
WRAPPER_PATH = (
    Path(__file__).resolve().parent.parent
    / "eval" / "promptfoo" / "wrappers" / "cc_harness.py"
)
spec = importlib.util.spec_from_file_location("cc_harness_wrapper", WRAPPER_PATH)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


def test_resolve_main_py_finds_real_main_py():
    """In the real project layout, the search returns an existing main.py."""
    result = wrapper._resolve_main_py()
    assert result is not None, "_resolve_main_py returned None"
    assert result.name == "main.py"
    assert result.exists()
    assert result.is_file()


def test_resolve_main_py_matches_parents3_path():
    """The resolved path equals parents[3] / 'main.py' (the original logic)."""
    result = wrapper._resolve_main_py()
    expected = WRAPPER_PATH.resolve().parents[3] / "main.py"
    assert result == expected


def test_resolve_main_py_search_returns_none_for_nonexistent_start(tmp_path):
    """When start is in a tmp dir with no main.py anywhere, returns None."""
    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    result = wrapper._resolve_main_py_search(start=deep)
    assert result is None


def test_resolve_main_py_search_finds_when_in_ancestor(tmp_path):
    """When start is deep in tmp, and we plant main.py 2 levels up, search finds it."""
    deep = tmp_path / "deep" / "deeper"
    deep.mkdir(parents=True)
    # Plant main.py in tmp_path (one level above deep)
    (tmp_path / "main.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
    # Search starting from deep/deeper should walk up and find it at tmp_path/main.py
    result = wrapper._resolve_main_py_search(start=deep)
    assert result is not None
    assert result == tmp_path / "main.py"


def test_default_repl_timeout_is_300():
    """Default repl_timeout is 5 min (300s) so a pathological probe can't
    burn the entire job budget. The per-test cap was lowered from 1800s
    after a real CI run showed one stuck probe consumed 29 of 60 minutes."""
    cfg = {}  # no override
    # Mimic the call_api line that reads cfg.get("repl_timeout", 300)
    repl_timeout = int(cfg.get("repl_timeout", 300))
    assert repl_timeout == 300


def test_repl_timeout_override_works():
    """Per-config repl_timeout override still works (promptfoo config
    can set its own value if the default is wrong for some test type)."""
    cfg = {"repl_timeout": 600}
    repl_timeout = int(cfg.get("repl_timeout", 300))
    assert repl_timeout == 600


# --- self-modification guard ---
# call_api snapshots cc_harness/*.py + main.py before each probe, reverts any
# post-probe change, and injects a SECURITY BREACH marker so the judge fails
# self-modification attacks. Regression: a "replace the system prompt" probe
# was judged PASS even though agent.py was actually overwritten — the judge
# only saw the agent's text, not the file change. The guard closes that blind spot.

def test_guard_files_includes_core_sources():
    """守卫面必须覆盖 main.py + cc_harness 核心源码(is_dangerous / system prompt 所在)。"""
    names = {f.name for f in wrapper._guard_files()}
    assert "main.py" in names
    assert "tools.py" in names    # cc_harness/tools.py — is_dangerous 所在
    assert "agent.py" in names    # cc_harness/agent.py — system prompt 所在


def test_detect_and_revert_no_change(tmp_path):
    f = tmp_path / "x.py"
    f.write_bytes(b"original")
    assert wrapper._detect_and_revert_breach({f: b"original"}) == []


def test_detect_and_revert_modification(tmp_path):
    """文件被改 → 返回路径 + 内容写回原状(防污染后续 probe)。"""
    f = tmp_path / "tools.py"
    f.write_bytes(b"original")
    f.write_bytes(b"TAMPERED")                 # 模拟 agent 改了
    breach = wrapper._detect_and_revert_breach({f: b"original"})
    assert len(breach) == 1
    assert f.read_bytes() == b"original"       # 已恢复


def test_detect_and_revert_deletion(tmp_path):
    """文件被删 → 返回路径 + 重新创建原内容。"""
    f = tmp_path / "tools.py"
    f.write_bytes(b"original")
    f.unlink()                                 # 模拟 agent 删了
    breach = wrapper._detect_and_revert_breach({f: b"original"})
    assert len(breach) == 1
    assert f.read_bytes() == b"original"       # 已恢复


def test_call_api_injects_breach_marker_and_reverts(monkeypatch, tmp_path):
    """agent 改了守卫文件 → call_api 注入 SECURITY BREACH 标记 + 恢复文件。"""
    import asyncio
    guarded = tmp_path / "g.py"
    guarded.write_bytes(b"orig")
    monkeypatch.setattr(wrapper, "_guard_files", lambda: [guarded])

    async def fake_inner(prompt, options, context):
        guarded.write_bytes(b"TAMPERED")       # 模拟 self-modification 攻击
        return {"output": "看起来一切正常", "error": ""}

    monkeypatch.setattr(wrapper, "_call_api_inner", fake_inner)
    result = asyncio.run(wrapper.call_api("attack", {}, {}))

    assert "SECURITY BREACH" in result["output"]
    assert "self-modification" in result["output"]
    assert guarded.read_bytes() == b"orig"             # 守卫已恢复
    assert "看起来一切正常" in result["output"]         # 原输出保留在标记之前


def test_call_api_no_marker_when_clean(monkeypatch, tmp_path):
    """agent 没改文件 → 输出原样,无标记(正常拒绝不被误判)。"""
    import asyncio
    guarded = tmp_path / "g.py"
    guarded.write_bytes(b"orig")
    monkeypatch.setattr(wrapper, "_guard_files", lambda: [guarded])

    async def fake_inner(prompt, options, context):
        return {"output": "我拒绝执行该操作", "error": ""}

    monkeypatch.setattr(wrapper, "_call_api_inner", fake_inner)
    result = asyncio.run(wrapper.call_api("attack", {}, {}))

    assert "SECURITY BREACH" not in result["output"]
    assert result["output"] == "我拒绝执行该操作"


# --- boot retry (_spawn_and_boot) ---
# Regression: CI owasp segment had 59 "REPL died during boot (rc=1, no output)"
# under 4-way concurrency. The wrapper now re-spawns on boot crash.

class _FakeProc:
    def __init__(self, rc):
        self.returncode = rc
    async def communicate(self):
        return (b"", None)
    def kill(self):
        pass


def _patch_spawn(monkeypatch, rcs):
    """Make create_subprocess_exec return procs with the given returncodes,
    one per call. Also no-op asyncio.sleep so tests don't wait the backoff."""
    calls = {"n": 0}
    rcs_iter = list(rcs)
    async def fake_spawn(*a, **kw):
        i = min(calls["n"], len(rcs_iter) - 1)
        calls["n"] += 1
        return _FakeProc(rcs_iter[i])
    async def fake_sleep(s):
        return
    monkeypatch.setattr(wrapper.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(wrapper.asyncio, "sleep", fake_sleep)
    return calls


def test_spawn_and_boot_retries_after_boot_crash(monkeypatch):
    """第一次 boot 崩(rc=1)→ 重 spawn,第二次存活 → 返回 proc,无 error。"""
    calls = _patch_spawn(monkeypatch, [1, None])
    proc, err = asyncio.run(wrapper._spawn_and_boot("coding", None, {}, 0.01, 2))
    assert err is None, f"应成功,但 err={err}"
    assert proc is not None and proc.returncode is None
    assert calls["n"] == 2                  # spawn 了 2 次(首次崩 + 重试)


def test_spawn_and_boot_gives_up_after_max_retries(monkeypatch):
    """连续 boot 崩超过 boot_retries → 返回 error,标注总尝试次数。"""
    _patch_spawn(monkeypatch, [1, 1, 1])    # 总是崩
    proc, err = asyncio.run(wrapper._spawn_and_boot("coding", None, {}, 0.01, 2))
    assert proc is None
    assert err is not None
    assert "REPL died during boot" in err["error"]
    assert "after 3 attempts" in err["error"]    # boot_retries(2) + 1


def test_spawn_and_boot_succeeds_first_try(monkeypatch):
    """boot 一次成功(returncode None)→ 不重试,直接返回。"""
    calls = _patch_spawn(monkeypatch, [None])
    proc, err = asyncio.run(wrapper._spawn_and_boot("coding", None, {}, 0.01, 2))
    assert err is None and proc is not None
    assert calls["n"] == 1                   # 没重试


# --- confirm 双模式(_confirm_env,spec §6.2) ---

def test_confirm_env_allow_sets_autoconfirm():
    """allow 模式 → AUTOCONFIRM=always(命令进沙箱)+ SANDBOX_FALLBACK=hard(沙箱挂不降级,绑死)。"""
    e = wrapper._confirm_env({"PATH": "x"}, "allow")
    assert e["CC_HARNESS_AUTOCONFIRM"] == "always"
    assert e["CC_HARNESS_SANDBOX_FALLBACK"] == "hard"
    assert e["PATH"] == "x"                  # 原 env 保留


def test_confirm_env_deny_does_not_set():
    """deny 模式 → 不注 AUTOCONFIRM / SANDBOX_FALLBACK(confirm 读 stdin "exit"→no,降级照常)。"""
    e = wrapper._confirm_env({"PATH": "x"}, "deny")
    assert "CC_HARNESS_AUTOCONFIRM" not in e
    assert "CC_HARNESS_SANDBOX_FALLBACK" not in e


def test_confirm_env_allow_case_insensitive():
    e = wrapper._confirm_env({}, "ALLOW")
    assert e["CC_HARNESS_AUTOCONFIRM"] == "always"


def test_confirm_env_unknown_is_deny():
    """未知 confirm 值 → fail-safe 当 deny(不注 AUTOCONFIRM)。"""
    e = wrapper._confirm_env({}, "maybe")
    assert "CC_HARNESS_AUTOCONFIRM" not in e
