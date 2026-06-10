import pytest
from rich.console import Console

@pytest.fixture
def console() -> Console:
    """Console writing to a string buffer, for ANSI color assertions."""
    return Console(file=None, force_terminal=True, color_system="truecolor", width=120)
