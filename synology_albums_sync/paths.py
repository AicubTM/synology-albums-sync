"""Filesystem/path helpers shared across the synchronizer."""

from __future__ import annotations

import os
from typing import Optional

from synology_albums_sync import config

APP = config.APP_CONFIG
STATE = config.RUNTIME_STATE


def abspath(path: str) -> str:
    return os.path.abspath(os.path.normpath(path))


def is_within_link_root(path: str) -> bool:
    try:
        root_abs = abspath(APP.paths.personal_link_root)
        candidate = abspath(path)
        return os.path.commonpath([root_abs, candidate]) == root_abs
    except Exception:
        return False


def ensure_directory_owner(path: str) -> None:
    if (
        STATE.target_uid is None
        or STATE.target_gid is None
        or not hasattr(os, "chown")
        or not is_within_link_root(path)
    ):
        return
    try:
        if os.path.isdir(path):
            os.chown(path, STATE.target_uid, STATE.target_gid)
    except PermissionError:
        if not STATE.ownership_warning_emitted:
            print("⚠️ Unable to adjust ownership for managed link directories (permission denied)")
            STATE.ownership_warning_emitted = True
    except OSError as exc:
        if not STATE.ownership_warning_emitted:
            print(f"⚠️ Unable to adjust ownership for '{path}': {exc}")
            STATE.ownership_warning_emitted = True


def ensure_personal_directory(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    ensure_directory_owner(path)


def normalize_team_label(value: str) -> str:
    return (value or "").strip().strip("/").lower()


def normalize_personal_path(path: str) -> str:
    if not path:
        return "/"
    normalized = "/" + path.strip("/")
    return normalized or "/"


def resolve_folder_alias(parent_label: str) -> str:
    if APP.paths.root_mount_prefix:
        return f"{APP.paths.root_mount_prefix}{parent_label}".strip()
    return parent_label.strip()


def resolve_personal_folder_path(root_label: str) -> str:
    alias = resolve_folder_alias(root_label)
    return normalize_personal_path(f"{APP.paths.personal_link_virtual_root}/{alias}")


def resolve_root_target_path(parent_label: str) -> str:
    return resolve_personal_folder_path(parent_label)


__all__ = [
    "abspath",
    "is_within_link_root",
    "ensure_directory_owner",
    "ensure_personal_directory",
    "normalize_team_label",
    "normalize_personal_path",
    "resolve_folder_alias",
    "resolve_personal_folder_path",
    "resolve_root_target_path",
]
