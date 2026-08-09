from pathlib import Path

import pytest
from PIL import Image

from cc_harness.terminal.attachments import AttachmentError, AttachmentManager


async def allow(path: Path) -> bool:
    return True


async def deny(path: Path) -> bool:
    return False


@pytest.mark.asyncio
async def test_text_and_image_mentions_become_real_message_parts(tmp_path):
    text_file = tmp_path / "hello world.py"
    text_file.write_text("print('hello')", encoding="utf-8")
    image_file = tmp_path / "screen.png"
    Image.new("RGB", (4, 3), "red").save(image_file)
    manager = AttachmentManager(tmp_path, (), tmp_path / ".cc-harness" / "a")

    content, attachments = await manager.prepare(
        '@"hello world.py" @screen.png explain', confirm_outside=deny,
    )

    assert isinstance(content, list)
    assert len(attachments) == 2
    assert {item.kind for item in attachments} == {"text", "image"}
    assert any(part["type"] == "image_url" for part in content)
    image_part = next(part for part in content if part["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    assert attachments[1].stored_path.is_file()


@pytest.mark.asyncio
async def test_outside_and_secret_attachments_are_blocked(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("no", encoding="utf-8")
    manager = AttachmentManager(project, (), project / ".cc-harness" / "a")
    with pytest.raises(AttachmentError, match="not approved"):
        await manager.prepare(f"@{outside}", confirm_outside=deny)

    secret = project / ".env"
    secret.write_text("KEY=x", encoding="utf-8")
    with pytest.raises(AttachmentError, match="secret"):
        await manager.prepare("@.env", confirm_outside=allow)


@pytest.mark.asyncio
async def test_long_multiline_prompt_is_not_probed_as_a_file_path(tmp_path):
    manager = AttachmentManager(tmp_path, (), tmp_path / ".cc-harness" / "a")
    prompt = "Bug report\n" + "A" * 5000

    content, attachments = await manager.prepare(prompt, confirm_outside=deny)

    assert content == prompt
    assert attachments == []


@pytest.mark.asyncio
async def test_code_decorators_are_not_treated_as_attachments(tmp_path):
    manager = AttachmentManager(tmp_path, (), tmp_path / ".cc-harness" / "a")
    prompt = '@unittest.skip("hello")\n@pytest.mark.skip\ndef test_example():\n    pass'

    content, attachments = await manager.prepare(prompt, confirm_outside=deny)

    assert content == prompt
    assert attachments == []
