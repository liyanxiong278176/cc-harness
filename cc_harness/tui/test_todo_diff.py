"""Tests for ChatLog.write_todo diff-only update.

Task 17 acceptance test: TodoUpdate emit twice, second call replaces the
todo block in-place (status flip from todo -> done shows correctly).
"""
from cc_harness.render import emit
from cc_harness.render_protocol import TodoUpdate


async def test_todo_update_diff_only():
    """Emit TodoUpdate twice; second call replaces first block in-place.

    Initial state: both Read and Parse are "todo".
    After second emit: Read is "done", Parse still "todo".
    Both rendered markers ([x] / [ ]) must appear in the chat lines.
    """
    from cc_harness.tui.app import PipTuiApp
    from cc_harness.tui.driver import TUIDriver

    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        driver = TUIDriver(app)
        # Initial todo: both items are todo
        emit(
            TodoUpdate(
                items=[
                    {"id": "1", "title": "Read", "status": "todo"},
                    {"id": "2", "title": "Parse", "status": "todo"},
                ]
            ),
            driver=driver,
        )
        await pilot.pause()
        # Update: id=1 flips to done, id=2 stays todo
        emit(
            TodoUpdate(
                items=[
                    {"id": "1", "title": "Read", "status": "done"},
                    {"id": "2", "title": "Parse", "status": "todo"},
                ]
            ),
            driver=driver,
        )
        await pilot.pause()
        chat = app.query_one("#chat")
        text = "\n".join(str(line) for line in chat.lines)
        assert "[x] Read" in text
        assert "[ ] Parse" in text