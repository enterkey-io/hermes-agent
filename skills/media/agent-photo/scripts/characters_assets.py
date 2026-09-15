#!/usr/bin/env python3
"""Bound, read-only access to identity references in the Characters app.

The trusted agent-photo wrapper owns authentication. This module accepts an
already-resolved token, enforces the profile-to-character binding, and writes
only validated images into the active profile's private cache.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import requests
from PIL import Image


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
CONTENT_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_IMAGE_BYTES = 25 * 1024 * 1024


class CharactersAssetError(RuntimeError):
    """A binding, API, ownership, or cache validation failed."""


def validate_binding(profile: str, binding: object) -> dict[str, str | None]:
    if not isinstance(binding, dict):
        raise CharactersAssetError("characters_profile_unbound")
    character_id = binding.get("character_id")
    seed_photo_id = binding.get("seed_photo_id")
    expected_name = binding.get("expected_name")
    if not isinstance(character_id, str) or not UUID_RE.fullmatch(character_id):
        raise CharactersAssetError("characters_binding_invalid")
    if seed_photo_id is not None and (
        not isinstance(seed_photo_id, str) or not UUID_RE.fullmatch(seed_photo_id)
    ):
        raise CharactersAssetError("characters_binding_invalid")
    if not isinstance(expected_name, str) or not expected_name.strip():
        raise CharactersAssetError("characters_binding_invalid")
    return {
        "profile": profile,
        "character_id": character_id,
        "seed_photo_id": seed_photo_id,
        "expected_name": expected_name.strip(),
    }


def public_photo(photo: object) -> dict[str, object]:
    if not isinstance(photo, dict):
        raise CharactersAssetError("characters_photo_metadata_invalid")
    photo_id = photo.get("id")
    character_id = photo.get("characterId")
    content_type = photo.get("contentType")
    if (
        not isinstance(photo_id, str)
        or not UUID_RE.fullmatch(photo_id)
        or not isinstance(character_id, str)
        or not UUID_RE.fullmatch(character_id)
        or content_type not in CONTENT_EXTENSIONS
    ):
        raise CharactersAssetError("characters_photo_metadata_invalid")
    return {
        "id": photo_id,
        "characterId": character_id,
        "role": photo.get("role") if isinstance(photo.get("role"), str) else None,
        "roleLabel": photo.get("roleLabel") if isinstance(photo.get("roleLabel"), str) else None,
        "caption": photo.get("caption") if isinstance(photo.get("caption"), str) else None,
        "contentType": content_type,
        "createdAt": photo.get("createdAt") if isinstance(photo.get("createdAt"), str) else None,
    }


class CharactersClient:
    def __init__(self, *, base_url: str, token: str, session: Any | None = None, timeout: int = 20) -> None:
        if not base_url.startswith("https://") or not token:
            raise CharactersAssetError("characters_client_invalid")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def _get(self, path: str, *, stream: bool = False):
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
                stream=stream,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            raise CharactersAssetError("characters_api_unavailable") from exc

    def photos(self, character_id: str) -> list[dict[str, object]]:
        if not UUID_RE.fullmatch(character_id):
            raise CharactersAssetError("characters_binding_invalid")
        response = self._get(f"/characters/{character_id}/photos")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CharactersAssetError("characters_photo_metadata_invalid") from exc
        raw_photos = payload.get("photos") if isinstance(payload, dict) else None
        if not isinstance(raw_photos, list):
            raise CharactersAssetError("characters_photo_metadata_invalid")
        photos = [public_photo(photo) for photo in raw_photos]
        if any(photo["characterId"] != character_id for photo in photos):
            raise CharactersAssetError("characters_photo_ownership_mismatch")
        return photos

    def character(self, *, character_id: str, expected_name: str) -> dict[str, object]:
        response = self._get("/characters")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CharactersAssetError("characters_character_metadata_invalid") from exc
        raw_characters = payload.get("characters") if isinstance(payload, dict) else None
        if not isinstance(raw_characters, list):
            raise CharactersAssetError("characters_character_metadata_invalid")
        matches = [
            item
            for item in raw_characters
            if isinstance(item, dict) and item.get("id") == character_id
        ]
        if len(matches) != 1 or matches[0].get("name") != expected_name:
            raise CharactersAssetError("characters_character_binding_mismatch")
        return {
            "id": character_id,
            "name": expected_name,
            "trainingPhotoCount": matches[0].get("trainingPhotoCount"),
        }

    def bound_photo(self, *, character_id: str, photo_id: str, required_role: str | None = None) -> dict[str, object]:
        if not UUID_RE.fullmatch(photo_id):
            raise CharactersAssetError("characters_photo_id_invalid")
        matches = [photo for photo in self.photos(character_id) if photo["id"] == photo_id]
        if len(matches) != 1:
            raise CharactersAssetError("characters_photo_not_bound")
        photo = matches[0]
        if required_role is not None and photo.get("role") != required_role:
            raise CharactersAssetError("characters_photo_role_mismatch")
        return photo

    def download(self, photo: dict[str, object]) -> bytes:
        response = self._get(f"/photos/{photo['id']}/file", stream=True)
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_IMAGE_BYTES:
                    raise CharactersAssetError("characters_image_too_large")
            except ValueError as exc:
                raise CharactersAssetError("characters_image_invalid") from exc
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise CharactersAssetError("characters_image_too_large")
            chunks.append(chunk)
        content = b"".join(chunks)
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
                actual_format = image.format
        except Exception as exc:
            raise CharactersAssetError("characters_image_invalid") from exc
        expected_format = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[str(photo["contentType"])]
        if actual_format != expected_format:
            raise CharactersAssetError("characters_image_type_mismatch")
        return content


def _private_cache_root(profile_root: Path) -> Path:
    profile_root = profile_root.resolve()
    assets = profile_root / "assets"
    if assets.is_symlink() or (assets.exists() and not assets.is_dir()):
        raise CharactersAssetError("characters_cache_path_refused")
    assets.mkdir(mode=0o700, exist_ok=True)
    os.chmod(assets, 0o700)
    cache = assets / "characters"
    if cache.is_symlink() or (cache.exists() and not cache.is_dir()):
        raise CharactersAssetError("characters_cache_path_refused")
    cache.mkdir(mode=0o700, exist_ok=True)
    os.chmod(cache, 0o700)
    if cache.resolve().parent != assets.resolve():
        raise CharactersAssetError("characters_cache_path_refused")
    return cache


def cache_photo(*, profile_root: Path, photo: dict[str, object], content: bytes, seed: bool) -> Path:
    cache = _private_cache_root(profile_root)
    if seed:
        target_dir = cache
        stem = "lifelike-seed"
    else:
        target_dir = cache / "references"
        if target_dir.is_symlink() or (target_dir.exists() and not target_dir.is_dir()):
            raise CharactersAssetError("characters_cache_path_refused")
        target_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(target_dir, 0o700)
        stem = str(photo["id"])
    extension = CONTENT_EXTENSIONS[str(photo["contentType"])]
    target = target_dir / f"{stem}{extension}"
    fd, temporary = tempfile.mkstemp(prefix=f".{stem}.", dir=target_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    metadata = {
        "schema_version": 1,
        "character_id": photo["characterId"],
        "photo_id": photo["id"],
        "role": photo.get("role"),
        "content_type": photo["contentType"],
        "relative_path": str(target.relative_to(profile_root.resolve())),
    }
    metadata_target = target.with_suffix(target.suffix + ".json")
    metadata_target.write_text(json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(metadata_target, 0o600)
    return target
