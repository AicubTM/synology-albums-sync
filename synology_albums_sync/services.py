"""Service layer helpers that support the album orchestration logic."""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from synology_albums_sync import config
from synology_albums_sync.media import (
    collect_target_paths,
    resolve_personal_child_paths,
    resolve_virtual_child_paths,
)
from synology_albums_sync.mounts import cleanup_root_mount, ensure_all_root_bind_mounts
from synology_albums_sync.synology_api import (
    ensure_photos_session,
    log_unindexed_paths,
    trigger_personal_reindex,
    wait_for_paths_indexed,
)

APP = config.APP_CONFIG
STATE = config.RUNTIME_STATE


class MediaService:
    """Encapsulates file-system media discovery helpers."""

    def resolve_child_paths(self, root_label: str, *, max_depth: Optional[int] = None) -> Tuple[List[str], str]:
        return resolve_virtual_child_paths(root_label, max_depth=max_depth)

    def collect_target_paths(self, root_names: Iterable[str]) -> List[str]:
        return collect_target_paths(root_names)

    def resolve_personal_child_paths(
        self,
        label: str,
        absolute_path: str,
        virtual_path: str,
        *,
        force_refresh: bool = False,
        max_depth: Optional[int] = None,
    ) -> Tuple[List[str], str]:
        return resolve_personal_child_paths(
            label,
            absolute_path,
            virtual_path,
            force_refresh=force_refresh,
            max_depth=max_depth,
        )


class MountService:
    """Handles bind-mount orchestration and DSM indexing waits."""

    def __init__(self, media_service: MediaService) -> None:
        self.media_service = media_service

    def ensure_mounts(self, wait_for_index: bool, *, label: str) -> List[str]:
        changed_roots = ensure_all_root_bind_mounts()
        if changed_roots:
            summary = ", ".join(changed_roots)
            print(f"🗂️ Ensured bind mounts for: {summary}")
        else:
            print("ℹ️ All managed bind mounts are already active; nothing to do.")

        if not wait_for_index:
            return changed_roots

        ensure_photos_session()
        target_paths = self.media_service.collect_target_paths(APP.sharing.target_roots.keys())
        needs_reindex = bool(changed_roots and APP.indexing.reindex_after_link)
        wait_label = label
        if needs_reindex:
            print("♻️ Triggering reindex after mount changes")
            if trigger_personal_reindex():
                wait_label = f"{label} (post-reindex)"

        pending_paths = wait_for_paths_indexed(target_paths, label=wait_label)
        log_unindexed_paths(wait_label, pending_paths)
        STATE.roots_awaiting_reindex.clear()
        STATE.clear_link_change_flag()
        return changed_roots

    def reindex_and_wait(self, label: str) -> None:
        ensure_photos_session()
        target_paths = self.media_service.collect_target_paths(APP.sharing.target_roots.keys())
        reindex_ran = False
        if APP.indexing.reindex_after_link:
            reindex_ran = trigger_personal_reindex()
        wait_label = f"{label} (after reindex)" if reindex_ran else f"{label} (passive wait)"
        pending_paths = wait_for_paths_indexed(target_paths, label=wait_label)
        log_unindexed_paths(wait_label, pending_paths)

    def unmount_roots_only(self) -> None:
        removed = 0
        for root_name in APP.sharing.target_roots:
            removed += cleanup_root_mount(root_name)
        if removed:
            print(f"🧹 Unmounted/cleaned {removed} managed mount point(s)")
        else:
            print("ℹ️ No managed mount points required cleanup")


__all__ = ["MediaService", "MountService"]
