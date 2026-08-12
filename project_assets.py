"""Content-addressed image assets owned by one Idea2Video run project."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any

from longform import LongFormError, LongProjectStore


MAX_IMAGE_BYTES = 10 * 1024 * 1024
DATA_URL_RE = re.compile(
    r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\r\n]+)$",
    re.IGNORECASE,
)
EXTENSION_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
MIME_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def validate_image_bytes(data: bytes, mime: str) -> bytes:
    normalized = str(mime or "").lower()
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise LongFormError(
            "每张项目图片必须小于等于 10 MiB。", "project_asset_size_invalid"
        )
    valid = bool(
        (normalized == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
        or (normalized == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
        or (
            normalized == "image/webp"
            and data.startswith(b"RIFF")
            and data[8:12] == b"WEBP"
        )
    )
    if not valid:
        raise LongFormError(
            "项目图片内容与声明格式不一致。", "project_asset_type_invalid"
        )
    return data


def decode_data_url(value: str) -> tuple[str, bytes]:
    match = DATA_URL_RE.fullmatch(str(value or ""))
    if not match:
        raise LongFormError(
            "只接受 PNG、JPEG 或 WebP 项目图片。", "project_asset_type_invalid"
        )
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LongFormError("项目图片 Base64 数据损坏。", "project_asset_invalid") from exc
    mime = match.group(1).lower()
    return mime, validate_image_bytes(data, mime)


def image_data_url(path: Path, mime: str | None = None) -> str:
    resolved_mime = str(mime or MIME_BY_EXTENSION.get(path.suffix.lower()) or "")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise LongFormError("项目图片不存在。", "project_asset_missing") from exc
    validate_image_bytes(data, resolved_mime)
    return f"data:{resolved_mime};base64," + base64.b64encode(data).decode("ascii")


class ProjectAssetStore:
    def __init__(self, store: LongProjectStore) -> None:
        self.store = store

    def _asset_directory(self, project_id: str, *, create: bool = False) -> Path:
        project_root = self.store.project_dir(project_id).resolve()
        directory = project_root / "assets"
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        resolved = directory.resolve(strict=False)
        try:
            resolved.relative_to(project_root)
        except ValueError as exc:
            raise LongFormError(
                "项目 assets 目录不能通过联接或符号链接越界。",
                "project_asset_path_invalid",
            ) from exc
        return resolved

    def save_data_url(
        self, project_id: str, *, original_name: str, data_url: str
    ) -> dict[str, Any]:
        mime, data = decode_data_url(data_url)
        return self.save_bytes(
            project_id, original_name=original_name, mime=mime, data=data
        )

    def save_bytes(
        self,
        project_id: str,
        *,
        original_name: str,
        mime: str,
        data: bytes,
    ) -> dict[str, Any]:
        self.store.load(project_id)
        normalized_mime = str(mime or "").lower()
        validate_image_bytes(data, normalized_mime)
        extension = EXTENSION_BY_MIME.get(normalized_mime)
        if extension is None:
            raise LongFormError(
                "项目图片格式不受支持。", "project_asset_type_invalid"
            )
        digest = hashlib.sha256(data).hexdigest()
        directory = self._asset_directory(project_id, create=True)
        destination = directory / f"{digest}{extension}"
        if not destination.is_file():
            temporary = directory / f".{digest}.{uuid.uuid4().hex}.part"
            try:
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            except Exception:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
        elif destination.read_bytes() != data:
            raise LongFormError(
                "项目图片哈希冲突。", "project_asset_hash_conflict"
            )
        safe_name = Path(str(original_name or destination.name)).name[:255]
        return {
            "source": "project_asset",
            "asset_id": digest,
            "relative_path": f"assets/{destination.name}",
            "mime": normalized_mime,
            "bytes": len(data),
            "original_name": safe_name,
        }

    def resolve(self, project_id: str, asset: dict[str, Any]) -> Path:
        if not isinstance(asset, dict) or asset.get("source") != "project_asset":
            raise LongFormError("项目图片引用无效。", "project_asset_invalid")
        asset_id = str(asset.get("asset_id") or "")
        relative = str(asset.get("relative_path") or "").replace("\\", "/")
        if not re.fullmatch(r"[0-9a-f]{64}", asset_id):
            raise LongFormError("项目图片 ID 无效。", "project_asset_invalid")
        if not re.fullmatch(
            rf"assets/{re.escape(asset_id)}\.(?:png|jpg|jpeg|webp)", relative
        ):
            raise LongFormError("项目图片路径无效。", "project_asset_invalid")
        asset_root = self._asset_directory(project_id)
        candidate = asset_root / Path(relative).name
        candidate = candidate.resolve()
        try:
            candidate.relative_to(asset_root)
        except ValueError as exc:
            raise LongFormError("项目图片路径越界。", "project_asset_invalid") from exc
        if not candidate.is_file():
            raise LongFormError("项目图片不存在。", "project_asset_missing")
        data = candidate.read_bytes()
        mime = str(asset.get("mime") or MIME_BY_EXTENSION.get(candidate.suffix.lower()) or "")
        validate_image_bytes(data, mime)
        if hashlib.sha256(data).hexdigest() != asset_id:
            raise LongFormError("项目图片哈希校验失败。", "project_asset_hash_invalid")
        return candidate

    def public(self, project_id: str, asset: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve(project_id, asset)
        return {
            **asset,
            "url": (
                f"/api/long/projects/{project_id}/assets/"
                f"{asset['asset_id']}"
            ),
            "filename": path.name,
        }

    def find(self, project_id: str, asset_id: str) -> tuple[Path, str]:
        if not re.fullmatch(r"[0-9a-f]{64}", str(asset_id or "")):
            raise LongFormError("项目图片 ID 无效。", "project_asset_invalid")
        self.store.load(project_id)
        directory = self._asset_directory(project_id)
        matches = [
            item
            for item in directory.glob(f"{asset_id}.*")
            if item.is_file() and item.suffix.lower() in MIME_BY_EXTENSION
        ]
        if len(matches) != 1:
            raise LongFormError("项目图片不存在。", "project_asset_not_found")
        path = matches[0]
        mime = MIME_BY_EXTENSION[path.suffix.lower()]
        data = path.read_bytes()
        validate_image_bytes(data, mime)
        if hashlib.sha256(data).hexdigest() != asset_id:
            raise LongFormError("项目图片哈希校验失败。", "project_asset_hash_invalid")
        return path, mime
