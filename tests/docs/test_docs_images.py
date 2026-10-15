"""Every image under `docs/assets` is a valid file: a line-ending conversion breaks images silently.

A PNG starts with its 8-byte signature (`\x89PNG\r\n\x1a\n`), a GIF with `GIF87a` or
`GIF89a`, a WebP with `RIFF....WEBP`. A conversion of CRLF to LF strips the `\r` of the PNG
signature, so the check finds a damaged file before the docs show a broken picture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[2] / "docs" / "assets"

pytestmark = pytest.mark.skipif(not ASSETS.is_dir(), reason="docs/ is in the repository only")


def _valid(path: Path) -> bool:
    head = path.read_bytes()[:12]
    suffix = path.suffix.lower()
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".gif":
        return head[:6] in (b"GIF87a", b"GIF89a")
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def test_every_image_in_docs_assets_has_a_valid_header() -> None:
    images = [p for p in ASSETS.rglob("*") if p.suffix.lower() in {".png", ".gif", ".webp"}]
    assert images, "docs/assets holds no images"
    broken = sorted(str(p.relative_to(ASSETS)) for p in images if not _valid(p))
    assert not broken, f"damaged images (check .gitattributes, capture them again): {broken}"


def test_images_are_binary_in_gitattributes() -> None:
    rules = (ASSETS.parents[1] / ".gitattributes").read_text(encoding="utf-8").split("\n")
    for pattern in ("*.png binary", "*.gif binary", "*.webp binary"):
        assert pattern in rules, pattern
