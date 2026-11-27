"""Validation rules for skill identifiers and stored resource names."""
from __future__ import annotations

import re

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_FORBIDDEN_RESOURCE_EXTS = (".py", ".sh", ".bash", ".zsh", ".exe", ".dll", ".so", ".bin", ".bat", ".ps1")


def validate_skill_slug(slug: str) -> str:
    cleaned = slug.strip().lower()
    if not _SLUG_RE.match(cleaned):
        raise ValueError("skill slug 须为 2~64 位小写字母、数字、连字符或下划线")
    return cleaned


def validate_resource_name(name: str) -> str:
    cleaned = name.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/") or ".." in cleaned.split("/") or "\x00" in cleaned:
        raise ValueError(f"非法资源路径: {name}")
    low = cleaned.lower()
    if any(low.endswith(ext) for ext in _FORBIDDEN_RESOURCE_EXTS):
        raise ValueError(f"安全限制：Skill 仅支持声明式文本资源，禁止上传可执行文件 ({name})")
    return cleaned
