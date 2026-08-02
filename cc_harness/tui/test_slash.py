"""Tests for slash commands dispatcher (/help / /theme / /resume / /clear).

Task 13 acceptance tests.
"""
from cc_harness.tui.app import PipTuiApp


async def test_help_command_pushes_help_screen():
    """ /help 应当在 screen stack 顶 push 一个 HelpScreen。"""
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await app._handle_slash_command("/help")
        await pilot.pause()
        # screen stack 应有 HelpScreen
        assert len(app.screen_stack) >= 1
        # 顶层 screen 应是 HelpScreen
        from cc_harness.tui.screens.help import HelpScreen
        assert isinstance(app.screen, HelpScreen)


async def test_theme_command_pushes_theme_screen():
    """/theme 应当 push 一个 ThemeScreen 到 screen stack。"""
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await app._handle_slash_command("/theme")
        await pilot.pause()
        from cc_harness.tui.screens.theme import ThemeScreen
        assert isinstance(app.screen, ThemeScreen)


async def test_clear_command_clears_chat():
    """/clear 应当清空 ChatLog 内容。"""
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        # 先写一些
        chat = app.query_one("#chat")
        chat.write("[green]test message[/green]")
        await pilot.pause()
        assert len(chat.lines) > 0
        # 然后 /clear
        await app._handle_slash_command("/clear")
        await pilot.pause()
        assert len(chat.lines) == 0