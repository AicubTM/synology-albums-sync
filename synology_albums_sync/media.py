"""Media discovery helpers for Synology Photos automation."""

from __future__ import annotations

import os
from typing import Iterable, List, Optional, Tuple

from synology_albums_sync import config
from synology_albums_sync.paths import (
    normalize_personal_path,
    resolve_folder_alias,
    resolve_root_target_path,
)

APP = config.APP_CONFIG
STATE = config.RUNTIME_STATE


def _effective_scan_depth(override: Optional[int]) -> Optional[int]:
    if override is None:
        return APP.media.scan_max_depth
    if override < 0:
        return None
    if override <= 0:
        return None
    return override


def _is_media_file(name: str) -> bool:
    _, extension = os.path.splitext(name)
    return extension.lower() in config.MEDIA_FILE_EXTENSIONS


def _contains_media_files(path: str, *, max_depth: Optional[int] = None) -> bool:
    depth_limit = max_depth
    pending: List[Tuple[str, int]] = [(path, 0)]
    while pending:
        current, depth = pending.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_file(follow_symlinks=False) and _is_media_file(entry.name):
                            return True
                        can_descend = depth_limit is None or depth < depth_limit
                        if entry.is_dir(follow_symlinks=False) and can_descend:
                            pending.append((entry.path, depth + 1))
                    except OSError:
                        continue
        except (FileNotFoundError, PermissionError, NotADirectoryError, OSError):
            continue
    return False


def _has_direct_media_files(path: str) -> bool:
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                try:
                    if entry.is_file(follow_symlinks=False) and _is_media_file(entry.name):
                        return True
                except OSError:
                    continue
    except (FileNotFoundError, PermissionError, NotADirectoryError, OSError):
        return False
    return False


def _collect_nested_media_children(
    path: str,
    virtual_path: str,
    *,
    depth: int = 1,
    max_depth: Optional[int] = None,
) -> List[str]:
    if max_depth is not None and depth > max_depth:
        return []
    collected: List[str] = []
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                name = entry.name.strip()
                if not name or not config.is_valid_root_name(name):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                child_absolute = entry.path
                child_virtual = normalize_personal_path(f"{virtual_path}/{name}")
                if _has_direct_media_files(child_absolute):
                    collected.append(child_virtual)
                    continue
                remaining_depth: Optional[int] = None
                if max_depth is not None:
                    remaining_depth = max_depth - depth
                    if remaining_depth <= 0:
                        continue
                if not _contains_media_files(child_absolute, max_depth=remaining_depth):
                    continue
                collected.extend(
                    _collect_nested_media_children(
                        child_absolute,
                        child_virtual,
                        depth=depth + 1,
                        max_depth=max_depth,
                    )
                )
    except (FileNotFoundError, PermissionError, NotADirectoryError, OSError):
        return []
    return collected


def resolve_virtual_child_paths(
    root_label: str,
    *,
    force_refresh: bool = False,
    absolute_root: Optional[str] = None,
    virtual_root: Optional[str] = None,
    cache_key: Optional[str] = None,
    max_depth: Optional[int] = None,
) -> Tuple[List[str], str]:
    cache_id = cache_key or root_label
    if not force_refresh and cache_id in STATE.root_child_media_paths:
        return (
            list(STATE.root_child_media_paths[cache_id]),
            STATE.root_child_media_states.get(cache_id, "ok"),
        )
    scan_depth = _effective_scan_depth(max_depth)
    if absolute_root is None:
        alias = resolve_folder_alias(root_label).strip("/\\")
        if not alias:
            STATE.root_child_media_paths[cache_id] = []
            STATE.root_child_media_states[cache_id] = "missing"
            return [], "missing"
        absolute_root = os.path.normpath(os.path.join(APP.paths.personal_link_root, alias))
        virtual_root = resolve_root_target_path(root_label)
    normalized_virtual_root = normalize_personal_path(virtual_root or "/")
    paths: List[str] = []
    scan_successful = False
    found_media = False
    try:
        with os.scandir(absolute_root) as iterator:
            scan_successful = True
            for entry in iterator:
                name = entry.name.strip()
                if not name or not config.is_valid_root_name(name):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                child_absolute = entry.path
                if not _contains_media_files(child_absolute, max_depth=scan_depth):
                    continue
                found_media = True
                normalized_child = normalize_personal_path(f"{normalized_virtual_root}/{name}")
                if _has_direct_media_files(child_absolute):
                    paths.append(normalized_child)
                    continue
                nested_paths = _collect_nested_media_children(
                    child_absolute,
                    normalized_child,
                    max_depth=scan_depth,
                )
                if nested_paths:
                    paths.extend(nested_paths)
    except FileNotFoundError:
        if root_label not in STATE.roots_without_media_warned:
            print(
                f"[INFO] Bind mount path '{absolute_root}' for root '{root_label}' is missing; skipping DSM wait until it exists"
            )
            STATE.roots_without_media_warned.add(root_label)
        STATE.root_child_media_paths[cache_id] = []
        STATE.root_child_media_states[cache_id] = "missing"
        return [], "missing"
    except PermissionError:
        print(f"[WARN] Permission denied while scanning '{absolute_root}'; falling back to root path for '{root_label}'")
        return [], "error"
    except NotADirectoryError:
        print(f"[WARN] Expected directory at '{absolute_root}' for '{root_label}', but found a file; skipping wait")
        return [], "missing"
    except OSError as exc:
        print(f"[WARN] Unable to scan '{absolute_root}' for '{root_label}': {exc}")
        STATE.root_child_media_paths[cache_id] = []
        STATE.root_child_media_states[cache_id] = "error"
        return [], "error"
    paths = sorted(dict.fromkeys(paths))
    if scan_successful and not paths:
        if root_label not in STATE.roots_without_media_warned:
            if found_media:
                depth_label = scan_depth if scan_depth is not None else "full depth"
                print(
                    f"[INFO] Media detected under '{absolute_root}' for root '{root_label}', but only inside nested folders beyond the configured scan depth ({depth_label}); skipping DSM wait for that root"
                )
            else:
                print(
                    f"[INFO] No media detected under '{absolute_root}' for root '{root_label}'; skipping DSM wait for that root"
                )
            STATE.roots_without_media_warned.add(root_label)
        STATE.root_child_media_paths[cache_id] = []
        STATE.root_child_media_states[cache_id] = "empty"
        return [], "empty"
    STATE.root_child_media_paths[cache_id] = list(paths)
    STATE.root_child_media_states[cache_id] = "ok"
    return paths, "ok"


def collect_target_paths(root_names: Iterable[str]) -> List[str]:
    targets: List[str] = []
    managed_roots = APP.sharing.target_roots
    for root_name in root_names:
        if root_name not in managed_roots:
            continue
        sentinel_paths, sentinel_state = resolve_virtual_child_paths(root_name, force_refresh=True)
        if sentinel_paths:
            targets.extend(sentinel_paths)
            continue
        if sentinel_state in {"empty", "missing"}:
            continue
        targets.append(resolve_root_target_path(root_name))
    return targets


def resolve_personal_child_paths(
    label: str,
    absolute_root: str,
    virtual_root: str,
    *,
    force_refresh: bool = False,
    max_depth: Optional[int] = None,
) -> Tuple[List[str], str]:
    cache_key = f"personal::{label}::{virtual_root}"
    return resolve_virtual_child_paths(
        label,
        force_refresh=force_refresh,
        absolute_root=absolute_root,
        virtual_root=virtual_root,
        cache_key=cache_key,
        max_depth=max_depth,
    )


__all__ = [
    "collect_target_paths",
    "resolve_personal_child_paths",
    "resolve_virtual_child_paths",
]
