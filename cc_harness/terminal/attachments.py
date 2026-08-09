"""Bounded text, directory, and image attachments."""
from __future__ import annotations

import base64
import hashlib
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageGrab

ConfirmOutside = Callable[[Path], Awaitable[bool]]

_MENTION_RE = re.compile(r'@(?:"([^"]+)"|\'([^\']+)\'|([^\s]+))')
_SECRET_NAMES = {".env", ".env.local", "id_rsa", "id_ed25519", "credentials.json"}
_IGNORED_PARTS = {".git", ".venv", "node_modules", "__pycache__"}
_IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


@dataclass(frozen=True)
class Attachment:
    source: Path
    stored_path: Path | None
    kind: str
    display_name: str
    message_part: dict


class AttachmentError(ValueError):
    pass


class AttachmentManager:
    MAX_TEXT_BYTES = 256 * 1024
    MAX_TOTAL_TEXT_BYTES = 512 * 1024
    MAX_IMAGE_BYTES = 20 * 1024 * 1024

    def __init__(self, project_root: Path, additional_dirs: tuple[Path, ...], session_dir: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.additional_dirs = tuple(Path(p).resolve() for p in additional_dirs)
        self.session_dir = Path(session_dir)

    async def prepare(
        self,
        text: str,
        *,
        confirm_outside: ConfirmOutside,
    ) -> tuple[object, list[Attachment]]:
        matches = [
            match for match in _MENTION_RE.finditer(text) if self._is_attachment_mention(match)
        ]
        paths = [next(group for group in match.groups() if group is not None) for match in matches]
        stripped = text
        # A dragged image path often arrives as the only quoted token.
        if not paths:
            candidate = text.strip().strip('"\'')
            if self._is_dragged_file(candidate):
                paths = [candidate]
        attachments: list[Attachment] = []
        text_parts: list[str] = []
        total_text = 0
        for raw in paths:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self.project_root / path
            path = path.resolve()
            if not self._is_allowed(path) and not await confirm_outside(path):
                raise AttachmentError(f"attachment outside workspace was not approved: {path}")
            if any(part in _IGNORED_PARTS for part in path.parts):
                raise AttachmentError(f"attachment is in an excluded directory: {path}")
            if path.name.lower() in _SECRET_NAMES:
                raise AttachmentError(f"likely secret file cannot be attached: {path.name}")
            if path.is_dir():
                index = self._directory_index(path)
                total_text += len(index.encode("utf-8"))
                text_parts.append(f"<directory path=\"{path}\">\n{index}\n</directory>")
                attachments.append(Attachment(path, None, "directory", path.name, {"type": "text", "text": index}))
                continue
            if not path.is_file():
                raise AttachmentError(f"attachment not found: {path}")
            image = self._load_image(path)
            if image is not None:
                attachments.append(image)
                continue
            data = path.read_bytes()
            if b"\x00" in data[:4096]:
                raise AttachmentError(f"unsupported binary attachment: {path.name}")
            if len(data) > self.MAX_TEXT_BYTES:
                raise AttachmentError(f"text attachment exceeds {self.MAX_TEXT_BYTES} bytes: {path.name}")
            decoded = data.decode("utf-8", errors="replace")
            total_text += len(data)
            text_parts.append(f"<file path=\"{path}\">\n{decoded}\n</file>")
            attachments.append(Attachment(path, None, "text", path.name, {"type": "text", "text": decoded}))
        if total_text > self.MAX_TOTAL_TEXT_BYTES:
            raise AttachmentError("combined text attachments exceed the context attachment budget")
        if not attachments:
            return text, []
        for match in reversed(matches):
            stripped = stripped[:match.start()] + f"[attached: {Path(next(g for g in match.groups() if g is not None)).name}]" + stripped[match.end():]
        content: list[dict] = [{"type": "text", "text": stripped + ("\n\n" + "\n\n".join(text_parts) if text_parts else "")}]
        content.extend(a.message_part for a in attachments if a.kind == "image")
        return content, attachments

    async def from_clipboard(self) -> Attachment:
        value = ImageGrab.grabclipboard()
        if value is None:
            raise AttachmentError("clipboard does not contain an image")
        if isinstance(value, list):
            image_paths = [Path(p) for p in value if Path(p).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif")]
            if not image_paths:
                raise AttachmentError("clipboard contains files, but no supported image")
            attachment = self._load_image(image_paths[0].resolve())
            if attachment is None:
                raise AttachmentError("clipboard image type is unsupported")
            return attachment
        if not isinstance(value, Image.Image):
            raise AttachmentError("platform clipboard image import is unavailable")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        target = self.session_dir / "clipboard.png"
        value.save(target, "PNG")
        attachment = self._load_image(target)
        if attachment is None:
            raise AttachmentError("failed to encode clipboard image")
        return attachment

    def _load_image(self, path: Path) -> Attachment | None:
        try:
            with Image.open(path) as image:
                image.verify()
                image_format = image.format
        except (OSError, SyntaxError):
            return None
        if image_format not in _IMAGE_TYPES:
            raise AttachmentError(f"unsupported image format: {image_format}")
        if image_format == "GIF":
            with Image.open(path) as gif:
                if getattr(gif, "n_frames", 1) > 1:
                    raise AttachmentError("animated GIF attachments are not supported")
        data = path.read_bytes()
        if len(data) > self.MAX_IMAGE_BYTES:
            raise AttachmentError(f"image exceeds {self.MAX_IMAGE_BYTES} bytes: {path.name}")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()[:12]
        target = self.session_dir / f"{digest}{path.suffix.lower()}"
        if target.resolve() != path.resolve():
            shutil.copy2(path, target)
        mime = _IMAGE_TYPES[image_format]
        url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        return Attachment(
            source=path,
            stored_path=target,
            kind="image",
            display_name=path.name,
            message_part={"type": "image_url", "image_url": {"url": url}},
        )

    def _is_allowed(self, path: Path) -> bool:
        roots = (self.project_root, *self.additional_dirs)
        return any(path.is_relative_to(root) for root in roots)

    def _is_attachment_mention(self, match: re.Match[str]) -> bool:
        if match.group(1) is not None or match.group(2) is not None:
            return True
        raw = match.group(3)
        if raw is None or any(character in raw for character in "(){}[],;"):
            return False
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        try:
            if candidate.exists():
                return True
        except OSError:
            return False
        return (
            Path(raw).is_absolute()
            or raw.startswith(("./", "../", ".\\", "..\\", "~"))
            or "/" in raw
            or "\\" in raw
        )

    @staticmethod
    def _is_dragged_file(candidate: str) -> bool:
        if not candidate or "\n" in candidate or "\r" in candidate or len(candidate) > 4096:
            return False
        try:
            return Path(candidate).expanduser().is_file()
        except OSError:
            return False

    @staticmethod
    def _directory_index(path: Path) -> str:
        entries = []
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:200]:
            if child.name in _IGNORED_PARTS:
                continue
            suffix = "/" if child.is_dir() else ""
            entries.append(child.name + suffix)
        return "\n".join(entries)
