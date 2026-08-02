"""Tests for HITL ModalScreen (yes/always/no confirm).

Task 16 acceptance test.
"""
from cc_harness.tui.app import PipTuiApp


async def test_hitl_modal_returns_choice():
    """HITLScreen push + Enter confirm: callback receives the selected choice.

    默认 RadioSet 选 "no"(安全默认);按 Enter 触发 Confirm button
    → dismiss("no") → callback 收到 "no"。
    """
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        from cc_harness.tui.screens.hitl import HITLScreen

        result: dict = {}

        async def cb(value):
            result["choice"] = value

        screen = HITLScreen(question="Run rm -rf /tmp?")
        await app.push_screen(screen, cb)
        await pilot.pause()
        # Confirm button 应当自动 focus — 按 Enter 触发 Button.Pressed
        await pilot.press("enter")
        await pilot.pause()
        assert result.get("choice") == "no"
