"""Bind-mount helpers shared across the synchronizer entrypoints."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import List, Optional

from synology_albums_sync import config
from synology_albums_sync.paths import (
    ensure_directory_owner,
    ensure_personal_directory,
    resolve_folder_alias,
)

APP = config.APP_CONFIG
STATE = config.RUNTIME_STATE


def _decode_mount_token(token: str) -> str:
    try:
        return bytes(token, "utf-8").decode("unicode_escape")
    except Exception:
        return token


def find_bind_mount_source(target_path: str) -> str | None:
    if os.name == "nt":
        return None
    normalized_target = os.path.abspath(target_path)
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.strip().split()
                if len(parts) < 2:
                    continue
                source = _decode_mount_token(parts[0])
                mount_point = _decode_mount_token(parts[1])
                if os.path.abspath(mount_point) == normalized_target:
                    return source
    except (FileNotFoundError, OSError):
        return None
    return None


def _resolve_umount_command() -> Optional[str]:
    if APP.mounts.umount_command:
        return APP.mounts.umount_command
    if os.name == "nt":
        return None
    return shutil.which("umount")


def bind_mount_matches_source(target_path: str, source_path: str) -> bool:
    if os.name == "nt":
        return False
    try:
        if not os.path.exists(target_path) or not os.path.exists(source_path):
            return False
        if not os.path.ismount(target_path):
            return False
        return os.path.samefile(target_path, source_path)
    except (OSError, ValueError):
        return False


def ensure_root_bind_mount(parent_label: str, folder_alias: str) -> bool:
    if not APP.mounts.enable_root_bind_mounts:
        return False
    if not APP.mounts.mount_command:
        print("[WARN] Bind mounts requested but 'mount' binary is unavailable")
        return False
    source_root = os.path.normpath(os.path.join(APP.paths.shared_photo_root, parent_label))
    if not os.path.exists(source_root):
        print(f"[WARN] Shared root missing for '{parent_label}' at '{source_root}'")
        return False
    target_path = os.path.normpath(os.path.join(APP.paths.personal_link_root, folder_alias))
    if os.path.islink(target_path):
        print(
            f"[WARN] Cannot bind mount '{target_path}' because it is still a symlink; remove it or run with --unmap first"
        )
        return False
    ensure_personal_directory(target_path)
    if bind_mount_matches_source(target_path, source_root):
        ensure_directory_owner(target_path)
        return False
    existing_source = find_bind_mount_source(target_path)
    if existing_source:
        print(
            f"[WARN] Mount point '{target_path}' already points to '{existing_source}'; skipping bind mount for '{parent_label}'"
        )
        return False
    try:
        try:
            entries = os.listdir(target_path)
        except OSError as exc:
            print(f"[WARN] Unable to inspect '{target_path}' before mounting: {exc}")
            return False
        if entries:
            print(f"[WARN] Mount point '{target_path}' is not empty; aborting bind mount for '{parent_label}'")
            return False
        subprocess.run(
            [APP.mounts.mount_command, "--bind", source_root, target_path],
            check=True,
            capture_output=True,
            text=True,
        )
        ensure_directory_owner(target_path)
        print(f"[OK] Bind-mounted {target_path} -> {source_root}")
        STATE.mark_link_change()
        return True
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        stdout = exc.stdout.strip() if exc.stdout else ""
        detail = stderr or stdout or str(exc)
        print(f"[ERROR] Failed to bind mount '{source_root}' -> '{target_path}': {detail}")
    except FileNotFoundError:
        print("[WARN] 'mount' command not found while attempting bind mount")
    except PermissionError as exc:
        print(f"[WARN] Permission denied while mounting '{source_root}' -> '{target_path}': {exc}")
    return False


def unmount_bind_mount(target_path: str, parent_label: str) -> bool:
    if os.name == "nt":
        return False
    umount_cmd = _resolve_umount_command()
    if not umount_cmd:
        print("[WARN] 'umount' command not found while attempting to detach bind mount")
        return False
    existing_source = find_bind_mount_source(target_path)
    if not existing_source:
        return False
    try:
        subprocess.run([umount_cmd, target_path], check=True, capture_output=True, text=True)
        print(f"[OK] Unmounted bind mount for '{parent_label}' at '{target_path}' (source was '{existing_source}')")
        STATE.mark_link_change()
        return True
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        stdout = exc.stdout.strip() if exc.stdout else ""
        detail = stderr or stdout or str(exc)
        if "busy" in detail.lower():
            print(f"[WARN] '{target_path}' is busy; attempting lazy unmount")
            try:
                subprocess.run(
                    [umount_cmd, "-l", target_path],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                print(
                    f"[OK] Lazy-unmounted bind mount for '{parent_label}' at '{target_path}' (source was '{existing_source}')"
                )
                STATE.mark_link_change()
                return True
            except subprocess.CalledProcessError as lazy_exc:
                lazy_detail = (lazy_exc.stderr or lazy_exc.stdout or str(lazy_exc)).strip()
                print(f"[ERROR] Lazy unmount failed for '{target_path}': {lazy_detail}")
        print(f"[ERROR] Failed to unmount '{target_path}': {detail}")
        return False
    except PermissionError as exc:
        print(f"[WARN] Permission denied while unmounting '{target_path}': {exc}")
    return False


def ensure_bind_mount_ready_for_run(parent_label: str) -> bool:
    if not APP.mounts.enable_root_bind_mounts:
        return False
    alias = resolve_folder_alias(parent_label)
    expected_source = os.path.normpath(os.path.join(APP.paths.shared_photo_root, parent_label))
    target_path = os.path.normpath(os.path.join(APP.paths.personal_link_root, alias))
    existing_source = find_bind_mount_source(target_path)
    if bind_mount_matches_source(target_path, expected_source):
        if existing_source and os.path.normpath(existing_source) != expected_source:
            print(
                f"[INFO] Bind mount for '{parent_label}' already points at '{existing_source}'; treating it as '{expected_source}'"
            )
        return False
    if existing_source and os.path.normpath(existing_source) == expected_source:
        return False
    if existing_source:
        print(
            f"[WARN] Bind mount for '{parent_label}' points to '{existing_source}' instead of '{expected_source}'; remounting"
        )
        if not unmount_bind_mount(target_path, parent_label):
            return False
    else:
        print(f"[INFO] Bind mount for '{parent_label}' is not active; applying it now")
    if ensure_root_bind_mount(parent_label, alias):
        STATE.roots_awaiting_reindex.add(parent_label)
        return True
    return False


def ensure_all_root_bind_mounts() -> List[str]:
    changed: List[str] = []
    for root_name in APP.sharing.target_roots:
        if ensure_bind_mount_ready_for_run(root_name):
            changed.append(root_name)
    return changed


def cleanup_root_mount(parent_label: str) -> int:
    alias = resolve_folder_alias(parent_label)
    target_dir = os.path.normpath(os.path.join(APP.paths.personal_link_root, alias))
    if not os.path.lexists(target_dir):
        print(f"[INFO] Mount path missing for '{parent_label}': {target_dir}")
        return 0

    removed = 0
    if not unmount_bind_mount(target_dir, parent_label):
        print(f"[INFO] No active bind mount found for '{parent_label}' at '{target_dir}'")
    if os.path.islink(target_dir):
        try:
            os.unlink(target_dir)
            removed = 1
            print(f"[OK] Removed root link '{target_dir}' for '{parent_label}'")
        except OSError as exc:
            print(f"[WARN] Unable to remove link '{target_dir}': {exc}")
        return removed
    if os.path.isdir(target_dir):
        try:
            entries = os.listdir(target_dir)
        except OSError as exc:
            print(f"[WARN] Unable to inspect '{target_dir}' during cleanup: {exc}")
            return removed
        if entries:
            return removed
        try:
            os.rmdir(target_dir)
            removed = 1
            print(f"[OK] Removed empty mount directory '{target_dir}' for '{parent_label}'")
        except OSError as exc:
            print(f"[WARN] Unable to remove '{target_dir}': {exc}")
    return removed


__all__ = [
    "bind_mount_matches_source",
    "cleanup_root_mount",
    "ensure_all_root_bind_mounts",
    "ensure_bind_mount_ready_for_run",
    "ensure_root_bind_mount",
    "find_bind_mount_source",
    "unmount_bind_mount",
]
