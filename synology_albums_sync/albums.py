"""Album orchestration and sharing services."""

from __future__ import annotations

import os
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple, TYPE_CHECKING

from synology_api import exceptions

from synology_albums_sync import config
from synology_albums_sync.mounts import cleanup_root_mount, ensure_bind_mount_ready_for_run
from synology_albums_sync.paths import normalize_personal_path, resolve_folder_alias, resolve_personal_folder_path
from synology_albums_sync.synology_api import (
    collect_direct_team_child_names,
    debug_dump_nearby_folder_filters,
    ensure_photos_session,
    fetch_existing_albums,
    find_team_root_entry,
    list_team_child_folders,
    load_folder_filters,
    log_unindexed_paths,
    request_targeted_reindex,
    share_album,
    trigger_personal_reindex,
    wait_for_folder_entry,
    wait_for_paths_indexed,
)

if TYPE_CHECKING:  # pragma: no cover - type checking only
    from synology_albums_sync.services import MediaService, MountService

APP = config.APP_CONFIG
STATE = config.RUNTIME_STATE


@dataclass
class AlbumState:
    existing_albums: List[dict]
    existing_names: Set[str]
    by_name: Dict[str, dict]
    by_folder: Dict[int, dict]


class AlbumService:
    """Coordinates album creation, sharing, and cleanup."""

    def __init__(
        self,
        media_service: "MediaService",
        mount_service: Optional["MountService"] = None,
    ) -> None:
        self.media_service = media_service
        self.mount_service = mount_service
        self.app_config = APP
        self.runtime_state = STATE

    def _normalize_personal_path_arg(self, path: str) -> Tuple[str, str]:
        personal_root = os.path.abspath(self.app_config.paths.personal_photos_root)
        candidate_abs = path if os.path.isabs(path) else os.path.join(personal_root, path)
        candidate_abs = os.path.abspath(candidate_abs)
        try:
            common_prefix = os.path.commonpath([candidate_abs, personal_root])
        except ValueError as exc:
            raise ValueError(
                f"Personal path '{candidate_abs}' must live on the same volume as '{personal_root}'"
            ) from exc
        if common_prefix != personal_root:
            raise ValueError(f"Personal path '{candidate_abs}' must reside under '{personal_root}'")
        rel_token = os.path.relpath(candidate_abs, personal_root).replace("\\", "/").strip("/")
        virtual_path = "/" + rel_token if rel_token else "/"
        return normalize_personal_path(candidate_abs), normalize_personal_path(virtual_path)

    def attach_mount_service(self, mount_service: "MountService") -> None:
        self.mount_service = mount_service

    def run_sync(self, *, manage_mounts: bool, allow_defer_on_mount: bool) -> None:
        state = self.build_album_state()
        if self.app_config.indexing.force_reindex_on_start:
            print("[INFO] Forced reindex requested before processing roots")
            if trigger_personal_reindex():
                pending_paths = wait_for_paths_indexed(
                    self.media_service.collect_target_paths(self.app_config.sharing.target_roots.keys()),
                    label="initial reindex",
                )
                log_unindexed_paths("initial reindex", pending_paths)

        for root_name, share_config in self.app_config.sharing.target_roots.items():
            print(f"\n[INFO] Processing root '{root_name}'")
            try:
                self._process_root(
                    root_name,
                    share_config,
                    state,
                    allow_defer_on_mount=allow_defer_on_mount,
                    manage_mounts=manage_mounts,
                )
            except ValueError as exc:
                print(f"[ERROR] {exc}")

        if manage_mounts and self.runtime_state.roots_awaiting_reindex:
            pending_roots = [
                root for root in self.runtime_state.roots_awaiting_reindex if root in self.app_config.sharing.target_roots
            ]
            if pending_roots:
                print(
                    "\n[INFO] Reindex required (new mounts or unindexed folders); triggering personal reindex before album synchronization"
                )
                if trigger_personal_reindex():
                    self.runtime_state.clear_link_change_flag()
                    pending_paths = wait_for_paths_indexed(
                        self.media_service.collect_target_paths(pending_roots),
                        label="post-mount reindex",
                    )
                    log_unindexed_paths("post-mount reindex", pending_paths)
                self.runtime_state.roots_awaiting_reindex.difference_update(pending_roots)
                for root_name in pending_roots:
                    print(f"\n[INFO] Re-processing root '{root_name}' after reindex")
                    try:
                        self._process_root(
                            root_name,
                            self.app_config.sharing.target_roots[root_name],
                            state,
                            allow_defer_on_mount=False,
                            manage_mounts=manage_mounts,
                        )
                    except ValueError as exc:
                        print(f"[ERROR] {exc}")

        for label, share_conf in self.app_config.sharing.personal_target_roots.items():
            print(f"\n[INFO] Processing personal root '{label}'")
            try:
                self._process_personal_root(label, share_conf, state, max_scan_depth=None)
            except ValueError as exc:
                print(f"[ERROR] {exc}")

        if self.runtime_state.link_changes_occurred:
            print("[INFO] Link changes detected; triggering personal reindex once")
            if trigger_personal_reindex():
                pending_paths = wait_for_paths_indexed(
                    self.media_service.collect_target_paths(self.app_config.sharing.target_roots.keys()),
                    label="final link-change reindex",
                )
                log_unindexed_paths("final link-change reindex", pending_paths)

    def run_personal_roots(
        self,
        *,
        share_with_override: Optional[List[str]] = None,
        share_roles_override: Optional[List[str]] = None,
        permission_override: Optional[str] = None,
        max_depth_override: Optional[int] = None,
        explicit_roots: Optional[Sequence[Tuple[str, Dict[str, object]]]] = None,
    ) -> None:
        if explicit_roots is not None:
            roots = list(explicit_roots)
        else:
            roots = list(self.app_config.sharing.personal_target_roots.items())
        if not roots:
            print("[INFO] No personal roots configured; nothing to process")
            return
        state = self.build_album_state()
        for label, share_conf in roots:
            merged_conf = deepcopy(share_conf)
            if share_with_override is not None:
                merged_conf["share_with"] = list(share_with_override)
            if share_roles_override is not None:
                merged_conf["share_roles"] = list(share_roles_override)
            if permission_override:
                merged_conf["permission"] = permission_override
            print(f"\n[INFO] Processing personal root '{label}'")
            try:
                self._process_personal_root(
                    label,
                    merged_conf,
                    state,
                    max_scan_depth=max_depth_override,
                )
            except ValueError as exc:
                print(f"[ERROR] {exc}")
        print("\n[INFO] Personal roots completed")

    def delete_personal_albums_only(
        self,
        labels: Optional[Sequence[str]] = None,
        explicit_roots: Optional[Sequence[Tuple[str, Dict[str, object]]]] = None,
    ) -> None:
        if explicit_roots is not None:
            roots = list(explicit_roots)
        else:
            roots = list(self.app_config.sharing.personal_target_roots.items())
        if labels is not None:
            # Filter roots to the requested labels
            label_set = set(labels)
            roots = [(label, conf) for label, conf in roots if label in label_set]
        if not roots:
            print("[INFO] No personal roots configured; nothing to delete")
            return

        state = self.build_album_state()
        total_deleted = 0
        for label, share_conf in roots:
            total_deleted += self._remove_albums_for_personal_root(label, share_conf, state)
        print(f"[INFO] Deleted {total_deleted} personal album(s); no folders or media were removed")

    def delete_albums_only(self) -> None:
        state = self.build_album_state()
        total_deleted = 0
        for root_name in self.app_config.sharing.target_roots:
            total_deleted += self._remove_albums_for_root(root_name, state)
        print(f"[INFO] Deleted {total_deleted} managed Team Space album(s); no folders or media were removed")

    def delete_album_by_name(self, album_name: str) -> None:
        name = album_name.strip()
        if not name:
            print("[ERROR] Album name is required")
            return
        state = self.build_album_state()
        matches = [album for album in state.existing_albums if album.get("name") == name]
        if not matches:
            print(f"[INFO] Album '{name}' not found; nothing to delete")
            return
        photos_client = STATE.photos
        if photos_client is None:
            print("[WARN] Unable to delete albums; Synology session unavailable")
            return
        removed = 0
        for album in matches:
            album_id = album.get("id")
            if album_id is None:
                continue
            try:
                photos_client.delete_album(album_id)
                print(f"[INFO] Removed album '{name}' (id {album_id})")
                removed += 1
                self._unregister_album_cache(album, state)
            except Exception as exc:
                print(f"[ERROR] Failed to delete album '{name}': {exc}")
        if removed:
            state.existing_albums[:] = [album for album in state.existing_albums if album.get("name") != name]
            state.existing_names.discard(name)

    def list_albums(self, target_path: Optional[str] = None) -> None:
        try:
            ensure_photos_session()
            load_folder_filters()
        except Exception as exc:
            print(f"[ERROR] Unable to load albums or folder filters: {exc}")
            return
        state = self.build_album_state()
        allowed_ids: Optional[Set[int]] = None
        normalized_target: Optional[str] = None
        if target_path:
            try:
                abs_target, virtual_target = self._normalize_personal_path_arg(target_path)
                normalized_target = abs_target
            except ValueError as exc:
                print(f"❌ {exc}")
                return
            allowed_ids = set()
            prefixes = [abs_target.rstrip("/") + "/", virtual_target.rstrip("/") + "/"]
            exacts = {abs_target, virtual_target}
            for entry in STATE.folder_filter_cache.values():
                name = normalize_personal_path(entry.get("name", ""))
                if name in exacts or any(name.startswith(p) for p in prefixes):
                    try:
                        allowed_ids.add(int(entry.get("id")))
                    except (TypeError, ValueError):
                        continue
            if not allowed_ids:
                print(f"[INFO] No folder_filter entries under '{virtual_target}' or '{abs_target}'; nothing to list")
                return
        total = 0
        for album in state.existing_albums:
            folder_ids = self._extract_album_folder_ids(album)
            if allowed_ids is not None:
                if not folder_ids or not any(fid in allowed_ids for fid in folder_ids):
                    continue
            folder_paths: List[str] = []
            for fid in folder_ids:
                entry = STATE.folder_filter_cache.get(fid)
                if not entry:
                    continue
                folder_paths.append(normalize_personal_path(entry.get("name", "")))
            album_id = album.get("id")
            name = album.get("name", "")
            if folder_paths:
                print(f"- {name} (id {album_id}) | folders: {', '.join(sorted(folder_paths))}")
            else:
                print(f"- {name} (id {album_id})")
            total += 1
        scope = f" under '{normalized_target}'" if normalized_target else ""
        print(f"[INFO] Listed {total} album(s){scope}")

    def unmap_all_roots_and_albums(self) -> None:
        state = self.build_album_state()
        total_deleted = 0
        total_unmounted = 0
        for root_name in self.app_config.sharing.target_roots:
            total_deleted += self._remove_albums_for_root(root_name, state)
            total_unmounted += cleanup_root_mount(root_name)
        print(f"[INFO] Removed {total_deleted} album(s) and cleaned {total_unmounted} mount(s)")

    def build_album_state(self) -> AlbumState:
        ensure_photos_session()
        existing_albums = fetch_existing_albums()
        existing_names: Set[str] = {album.get("name", "") for album in existing_albums if album.get("name")}
        by_name: Dict[str, dict] = {}
        by_folder: Dict[int, dict] = {}
        for album in existing_albums:
            self._register_album_cache(album, by_name, by_folder)
        return AlbumState(existing_albums, existing_names, by_name, by_folder)

    def _process_root(
        self,
        parent_label: str,
        share_conf: Dict[str, object],
        state: AlbumState,
        *,
        allow_defer_on_mount: bool,
        manage_mounts: bool,
    ) -> None:
        target_path = resolve_personal_folder_path(parent_label)
        _, absolute_root_path = self._mount_paths_for_root(parent_label)
        mount_created = False
        if self.app_config.mounts.enable_root_bind_mounts:
            if manage_mounts:
                mount_created = ensure_bind_mount_ready_for_run(parent_label)
            else:
                if not os.path.ismount(absolute_root_path):
                    print(
                        f"[WARN] Bind mount for '{parent_label}' is missing at '{absolute_root_path}'; run with --mount first"
                    )
                    return
        else:
            if not os.path.exists(absolute_root_path):
                print(
                    f"[WARN] Personal path '{absolute_root_path}' is missing for '{parent_label}'. Create it or enable bind mounts."
                )
                return
        if self.app_config.mounts.enable_root_bind_mounts and mount_created:
            if allow_defer_on_mount:
                print(f"[INFO] Mounted '{parent_label}' into personal space; deferring album sync until after reindex")
                return
            print(f"[INFO] '{parent_label}' was freshly mounted earlier; continuing after reindex")

        media_child_paths, media_state = self.media_service.resolve_child_paths(parent_label)
        if media_state == "empty":
            print(f"[INFO] Root '{parent_label}' is empty; skipping indexing and album sync")
            return

        managed_child_paths: Set[str] = {
            normalize_personal_path(path).lower() for path in media_child_paths if path
        }
        relative_display_map: Dict[str, str] = {}
        for child_path in media_child_paths:
            relative_name = self._derive_relative_child_path(child_path, target_path)
            if not relative_name:
                continue
            key = relative_name.lower()
            relative_display_map.setdefault(key, relative_name)

        team_root = find_team_root_entry(parent_label)
        if not team_root:
            print(f"[WARN] Unable to find Team Space root '{parent_label}'")
            return

        team_children = list_team_child_folders(team_root.get("id"))
        team_child_names = collect_direct_team_child_names(parent_label, team_children)
        if not team_children:
            print(f"[WARN] No accessible Team folders under '{parent_label}'")
        self._synchronize_album_children(
            parent_label,
            share_conf,
            state,
            target_path=target_path,
            absolute_root_path=absolute_root_path,
            media_child_paths=media_child_paths,
            managed_child_paths=managed_child_paths,
            relative_display_map=relative_display_map,
            missing_reference_names=team_child_names,
            allow_defer_on_mount=allow_defer_on_mount,
        )

    def _process_personal_root(
        self,
        parent_label: str,
        share_conf: Dict[str, object],
        state: AlbumState,
        *,
        max_scan_depth: Optional[int] = None,
    ) -> None:
        raw_path = str(share_conf.get("personal_path") or "").strip()
        if not raw_path:
            print(f"[WARN] Personal root '{parent_label}' is missing 'personal_path'; skipping")
            return
        absolute_root_path = os.path.normpath(raw_path)
        if not os.path.isdir(absolute_root_path):
            print(f"[WARN] Personal root path '{absolute_root_path}' for '{parent_label}' does not exist; skipping")
            return
        relative_virtual = str(share_conf.get("relative_virtual_path") or "").strip("/")
        virtual_root = f"/{relative_virtual}" if relative_virtual else "/"
        target_path = normalize_personal_path(virtual_root)
        media_child_paths, media_state = self.media_service.resolve_personal_child_paths(
            parent_label,
            absolute_root_path,
            target_path,
            max_depth=max_scan_depth,
        )
        if media_state == "empty":
            print(f"[INFO] Personal root '{parent_label}' contains no folders with media; skipping album sync")
            return
        managed_child_paths: Set[str] = {
            normalize_personal_path(path).lower() for path in media_child_paths if path
        }
        relative_display_map: Dict[str, str] = {}
        for child_path in media_child_paths:
            relative_name = self._derive_relative_child_path(child_path, target_path)
            if not relative_name:
                continue
            key = relative_name.lower()
            relative_display_map.setdefault(key, relative_name)
        self._synchronize_album_children(
            parent_label,
            share_conf,
            state,
            target_path=target_path,
            absolute_root_path=absolute_root_path,
            media_child_paths=media_child_paths,
            managed_child_paths=managed_child_paths,
            relative_display_map=relative_display_map,
            missing_reference_names=None,
            allow_defer_on_mount=False,
        )

    def _synchronize_album_children(
        self,
        parent_label: str,
        share_conf: Dict[str, object],
        state: AlbumState,
        *,
        target_path: str,
        absolute_root_path: Optional[str],
        media_child_paths: Sequence[str],
        managed_child_paths: Set[str],
        relative_display_map: Dict[str, str],
        missing_reference_names: Optional[Sequence[str]],
        allow_defer_on_mount: bool,
    ) -> None:
        root_entry = self._find_folder_entry_by_path(target_path)
        if root_entry is None:
            load_folder_filters()
            root_entry = self._find_folder_entry_by_path(target_path)

        root_id = self._resolve_folder_id(root_entry) if root_entry else None
        children: List[dict] = []
        used_path_scan = False

        if root_entry is not None and root_id is not None:
            children = self._list_child_folder_filters(root_id)

        if not children:
            tentative_children = self._list_child_folder_filters_by_path(target_path)
            if tentative_children:
                children = tentative_children
                used_path_scan = True
            else:
                if root_entry is None:
                    root_entry = wait_for_folder_entry(target_path)
                    root_id = self._resolve_folder_id(root_entry) if root_entry else None
                    if root_entry is not None and root_id is not None:
                        children = self._list_child_folder_filters(root_id)
                if not children:
                    tentative_children = self._list_child_folder_filters_by_path(target_path)
                    if not tentative_children:
                        print(
                            f"[WARN] Unable to find indexed subfolders under '{target_path}'; skipping '{parent_label}'"
                        )
                        return
                    children = tentative_children
                    used_path_scan = True

        if used_path_scan:
            print(
                f"[INFO] Personal folder '{target_path}' missing from folder_filter; using path-prefix scan for '{parent_label}'"
            )

        if not children:
            print(f"[WARN] No indexed subfolders under '{target_path}'")

        preferred_children: List[dict] = []
        if media_child_paths:
            preferred_children, _ = self._resolve_folder_entries_for_paths(media_child_paths)
            if preferred_children:
                children = preferred_children

        active_folder_ids: Set[int] = set()
        expected_album_names: Set[str] = set()
        indexed_label_names: Set[str] = set()
        indexed_relative_names: Set[str] = set()

        for folder in children:
            child_label = self._folder_label(folder)
            normalized_child = child_label.strip().lower()
            if normalized_child:
                indexed_label_names.add(normalized_child)
            folder_path_value = folder.get("name", "")
            relative_token = self._derive_relative_child_path(folder_path_value, target_path)
            if relative_token:
                indexed_relative_names.add(relative_token.lower())

        if relative_display_map:
            missing_children = sorted(
                relative_display_map[token]
                for token in relative_display_map.keys()
                if token not in indexed_relative_names
            )
        elif missing_reference_names:
            missing_children = sorted(
                name for name in missing_reference_names if name not in indexed_label_names
            )
        else:
            missing_children = []

        if missing_children:
            preview = ", ".join(missing_children[:5])
            extra = "" if len(missing_children) <= 5 else f" +{len(missing_children) - 5} more"
            print(
                f"[WARN] Synology has not indexed {len(missing_children)} folder(s) under '{parent_label}' yet (e.g., {preview}{extra})."
            )
            if allow_defer_on_mount:
                self.runtime_state.roots_awaiting_reindex.add(parent_label)
            else:
                print("[WARN] Reindex already attempted; verify DSM indexing status manually if folders stay missing.")
            if absolute_root_path and os.path.exists(absolute_root_path):
                request_targeted_reindex(absolute_root_path)

        for folder in children:
            folder_id = self._resolve_folder_id(folder)
            if folder_id is not None:
                active_folder_ids.add(folder_id)
            child_label = self._folder_label(folder)
            album_name = self._sanitize_album_name(parent_label, child_label)
            expected_album_names.add(album_name)

            existing_album = self._find_album_for_folder(
                folder_id,
                album_name,
                state.by_name,
                state.by_folder,
            )
            if existing_album:
                existing_name = existing_album.get("name", album_name)
                if existing_name != album_name:
                    print(
                        f"[INFO] Folder '{child_label}' already managed via album '{existing_name}'; keeping existing name"
                    )
                album_id = existing_album.get("id")
                if album_id is None:
                    print(f"[WARN] Existing album entry for '{existing_name}' lacks an id; skipping share refresh")
                    continue
                state.existing_names.add(existing_name)
                share_album(
                    int(album_id),
                    existing_name,
                    share_conf.get("share_with", []),
                    share_conf.get("permission", "view"),
                    share_conf.get("share_roles", []),
                )
                continue

            result = self._create_album_for_folder(
                parent_label,
                folder,
                state,
            )
            if not result:
                continue
            album_id, album_name, cache_entry = result
            state.existing_albums.append(cache_entry)
            share_album(
                album_id,
                album_name,
                share_conf.get("share_with", []),
                share_conf.get("permission", "view"),
                share_conf.get("share_roles", []),
            )

        self._prune_removed_albums(
            parent_label,
            target_path,
            active_folder_ids,
            expected_album_names,
            state,
            managed_child_paths,
        )

    @staticmethod
    def _folder_label(entry: dict) -> str:
        raw_name = entry.get("name") or entry.get("display_name") or ""
        tokens = [token for token in raw_name.split("/") if token]
        return tokens[-1] if tokens else raw_name or str(entry.get("id", ""))

    @staticmethod
    def _resolve_folder_id(entry: Optional[dict]) -> Optional[int]:
        if not entry:
            return None
        for key in ("id", "folder_id", "item_id"):
            value = entry.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _extract_album_folder_ids(album: dict) -> List[int]:
        condition = album.get("condition", {}) or {}
        folder_ids = condition.get("folder_filter") or []
        extracted: List[int] = []
        for raw_id in folder_ids:
            try:
                extracted.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        return extracted

    def _register_album_cache(self, album: dict, by_name: Dict[str, dict], by_folder: Dict[int, dict]) -> None:
        name = album.get("name")
        if isinstance(name, str) and name:
            by_name[name] = album
        for folder_id in self._extract_album_folder_ids(album):
            by_folder[folder_id] = album

    def _unregister_album_cache(self, album: dict, state: AlbumState) -> None:
        name = album.get("name")
        if isinstance(name, str) and name:
            state.by_name.pop(name, None)
            state.existing_names.discard(name)
        for folder_id in self._extract_album_folder_ids(album):
            state.by_folder.pop(folder_id, None)
        try:
            state.existing_albums.remove(album)
        except ValueError:
            pass

    @staticmethod
    def _mount_paths_for_root(parent_label: str) -> Tuple[str, str]:
        alias = resolve_folder_alias(parent_label)
        absolute_root_path = os.path.normpath(os.path.join(APP.paths.personal_link_root, alias))
        return alias, absolute_root_path

    @staticmethod
    def _derive_relative_child_path(child_path: str, root_path: str) -> Optional[str]:
        child_normalized = normalize_personal_path(child_path)
        root_normalized = normalize_personal_path(root_path)
        if child_normalized == root_normalized:
            return None
        if not child_normalized.startswith(root_normalized.rstrip("/") + "/"):
            return None
        remainder = child_normalized[len(root_normalized.rstrip("/")) + 1 :]
        if not remainder:
            return None
        return remainder.split("/", 1)[0]

    @staticmethod
    def _is_direct_child_path(child_path: str, parent_path: str) -> Tuple[bool, Optional[str]]:
        child_normalized = normalize_personal_path(child_path)
        parent_normalized = normalize_personal_path(parent_path)
        if child_normalized == parent_normalized:
            return False, None
        if not child_normalized.startswith(parent_normalized.rstrip("/") + "/"):
            return False, None
        remainder = child_normalized[len(parent_normalized.rstrip("/")) + 1 :]
        if not remainder or "/" in remainder:
            return False, None
        return True, remainder

    def _list_child_folder_filters(self, parent_id: Optional[int]) -> List[dict]:
        if parent_id is None:
            return []
        return list(self.runtime_state.folder_filter_children.get(parent_id, []))

    def _list_child_folder_filters_by_path(self, parent_path: str) -> List[dict]:
        normalized_parent = normalize_personal_path(parent_path)
        children: List[dict] = []
        for entry in self.runtime_state.folder_filter_cache.values():
            is_direct, _ = self._is_direct_child_path(entry.get("name", ""), normalized_parent)
            if is_direct:
                children.append(entry)
        children.sort(key=lambda item: item.get("name", "").lower())
        return children

    def _resolve_folder_entries_for_paths(self, paths: Sequence[str]) -> Tuple[List[dict], List[str]]:
        resolved: List[dict] = []
        missing: List[str] = []
        for path in paths:
            normalized = normalize_personal_path(path)
            entry = self.runtime_state.folder_filter_path_index.get(normalized.lower())
            if entry:
                resolved.append(entry)
            else:
                missing.append(path)
        if missing:
            debug_dump_nearby_folder_filters(missing[0])
        return resolved, missing

    def _find_folder_entry_by_path(self, path: str) -> Optional[dict]:
        normalized = normalize_personal_path(path).lower()
        return self.runtime_state.folder_filter_path_index.get(normalized)

    @staticmethod
    def _parent_entry_exists(parent_id: object) -> bool:
        try:
            parent_int = int(parent_id)
        except (TypeError, ValueError):
            return False
        return parent_int in STATE.folder_filter_cache

    def _prepare_folder_filter_entry(self, folder: dict) -> Tuple[dict, bool]:
        entry = deepcopy(folder)
        owner_id = entry.get("owner_user_id") or self.runtime_state.current_user_id
        entry["owner_user_id"] = owner_id
        parent_id = entry.get("parent")
        removed_parent = False
        if parent_id is not None and not self._parent_entry_exists(parent_id):
            entry.pop("parent", None)
            removed_parent = True
        minimal_entry = {
            "id": entry.get("id"),
            "name": entry.get("name"),
            "owner_user_id": entry.get("owner_user_id"),
            "shared": entry.get("shared", False),
        }
        if "sort_by" in entry:
            minimal_entry["sort_by"] = entry["sort_by"]
        if "sort_direction" in entry:
            minimal_entry["sort_direction"] = entry["sort_direction"]
        return minimal_entry, removed_parent

    @staticmethod
    def _sanitize_album_name(parent_label: str, child_label: str) -> str:
        parent = (parent_label or "").strip()
        child = (child_label or "").strip()
        if parent and child:
            base = f"{parent} - {child}"
        elif parent:
            base = parent
        else:
            base = child
        base = re.sub(r"\s+", " ", base.replace("_", " "))
        return base.strip()

    def _prune_removed_albums(
        self,
        parent_label: str,
        target_path: str,
        active_folder_ids: Set[int],
        expected_album_names: Set[str],
        state: AlbumState,
        managed_child_paths: Optional[Set[str]] = None,
    ) -> None:
        orphans: List[dict] = []
        for album in state.existing_albums:
            if album.get("type") != "condition":
                continue
            album_name = album.get("name", "")
            album_condition = album.get("condition", {}) or {}
            folder_ids = album_condition.get("folder_filter") or []
            if len(folder_ids) != 1:
                continue
            folder_id = folder_ids[0]
            if folder_id in active_folder_ids:
                continue
            folder_entry = self.runtime_state.folder_filter_cache.get(folder_id)
            if not folder_entry:
                if album_name in expected_album_names:
                    continue
                print(
                    f"[INFO] Skipping album '{album_name}' (id {album.get('id')}) during pruning because folder id {folder_id} is missing from folder_filter cache"
                )
                continue
            folder_path = folder_entry.get("name", "")
            normalized_folder_path = normalize_personal_path(folder_path).lower()
            if managed_child_paths:
                if normalized_folder_path not in managed_child_paths:
                    continue
            else:
                is_direct, _ = self._is_direct_child_path(folder_path, target_path)
                if not is_direct:
                    continue
            orphans.append(album)
        if not orphans:
            return
        deleted_ids: Set[int] = set()
        for album in orphans:
            album_id = album.get("id")
            album_name = album.get("name", "")
            if album_id is None:
                continue
            photos_client = STATE.photos
            if photos_client is None:
                print("[WARN] Cannot delete albums; Synology session unavailable")
                break
            try:
                photos_client.delete_album(album_id)
                print(f"[INFO] Removed orphan album '{album_name}' (id {album_id})")
                state.existing_names.discard(album_name)
                deleted_ids.add(album_id)
                self._unregister_album_cache(album, state)
            except Exception as exc:
                print(f"[ERROR] Failed to delete album '{album_name}': {exc}")
        if deleted_ids:
            state.existing_albums[:] = [album for album in state.existing_albums if album.get("id") not in deleted_ids]

    def _create_album_for_folder(
        self,
        parent_label: str,
        folder: dict,
        state: AlbumState,
    ) -> Optional[Tuple[int, str, dict]]:
        if self.runtime_state.current_user_id is None:
            raise RuntimeError("User id unavailable; login sequence incomplete")
        child_label = self._folder_label(folder)
        if child_label.lower() in config.RESERVED_NAMES:
            return None
        album_name = self._sanitize_album_name(parent_label, child_label)
        if album_name in state.existing_names:
            print(f"[WARN] Album '{album_name}' already exists; skipping")
            return None
        folder_id = self._resolve_folder_id(folder)
        if folder_id is None:
            print(f"[WARN] Could not resolve folder id for entry '{child_label}'; skipping")
            return None
        folder_filter_entry, removed_parent = self._prepare_folder_filter_entry(folder)
        if removed_parent:
            print(
                f"[WARN] Folder id {folder_id} lacks parent entry in folder_filter; removed parent from album condition"
            )
        owner_id = folder_filter_entry.get("owner_user_id", self.runtime_state.current_user_id)
        condition = {
            "user_id": owner_id,
            "item_type": [],
            "folder_filter": [folder_id],
        }
        print(f"[INFO] Creating album '{album_name}' for folder id {folder['id']}")
        photos_client = STATE.photos
        if photos_client is None:
            print("[ERROR] Unable to create album; Synology session not initialized")
            return None
        try:
            album_response = photos_client.create_album(album_name, condition)
        except exceptions.PhotosError as api_exc:
            code = getattr(api_exc, "error_code", "?")
            print(f"[ERROR] Failed to create '{album_name}' (folder {folder['id']}), error code {code}: {api_exc}")
            print(f"    [DEBUG] Folder entry: {folder}")
            print(f"    [DEBUG] Condition: {condition}")
            return None
        except Exception as generic_exc:
            print(f"[ERROR] Unexpected error while creating '{album_name}': {generic_exc}")
            return None
        album = album_response["data"]["album"]
        album_id = album["id"]
        print(f"[INFO] Created album '{album_name}' (id {album_id})")
        state.existing_names.add(album_name)
        cache_entry = {
            "id": album_id,
            "name": album_name,
            "type": "condition",
            "condition": deepcopy(condition),
        }
        self._register_album_cache(cache_entry, state.by_name, state.by_folder)
        return album_id, album_name, cache_entry

    def _find_album_for_folder(
        self,
        folder_id: Optional[int],
        expected_name: str,
        by_name: Dict[str, dict],
        by_folder: Dict[int, dict],
    ) -> Optional[dict]:
        if expected_name and expected_name in by_name:
            return by_name[expected_name]
        if folder_id is not None:
            return by_folder.get(folder_id)
        return None

    def _remove_albums_for_personal_root(
        self,
        parent_label: str,
        share_conf: Dict[str, object],
        state: AlbumState,
    ) -> int:
        target_path = normalize_personal_path(str(share_conf.get("relative_virtual_path") or "") or "/")
        personal_path = os.path.normpath(str(share_conf.get("personal_path") or ""))
        media_child_paths, media_state = self.media_service.resolve_personal_child_paths(
            parent_label,
            personal_path,
            target_path,
            force_refresh=True,
        )
        if media_state in {"missing", "error"}:
            return 0
        allowed_paths: Set[str] = {normalize_personal_path(path).lower() for path in media_child_paths}
        folder_entries, _ = self._resolve_folder_entries_for_paths(media_child_paths)
        allowed_ids: Set[int] = set()
        for entry in folder_entries:
            try:
                allowed_ids.add(int(entry.get("id")))
            except (TypeError, ValueError):
                continue
        return self._remove_albums_for_paths(allowed_paths, allowed_ids, state)

    def _remove_albums_for_paths(
        self,
        allowed_paths: Set[str],
        allowed_ids: Set[int],
        state: AlbumState,
    ) -> int:
        photos_client = STATE.photos
        if photos_client is None:
            print("[WARN] Unable to delete albums; Synology session unavailable")
            return 0
        removed = 0
        for album in list(state.existing_albums):
            if album.get("type") != "condition":
                continue
            folder_ids = self._extract_album_folder_ids(album)
            if len(folder_ids) != 1:
                continue
            folder_id = folder_ids[0]
            path_ok = False
            if folder_id in allowed_ids:
                path_ok = True
            else:
                entry = STATE.folder_filter_cache.get(folder_id)
                if entry:
                    folder_path = normalize_personal_path(entry.get("name", "")).lower()
                    if folder_path in allowed_paths:
                        path_ok = True
            if not path_ok:
                continue
            album_id = album.get("id")
            if album_id is None:
                continue
            try:
                photos_client.delete_album(album_id)
                print(f"[INFO] Removed album '{album.get('name', '')}' (id {album_id})")
                removed += 1
                self._unregister_album_cache(album, state)
            except Exception as exc:
                print(f"[ERROR] Failed to delete album '{album.get('name', '')}': {exc}")
        return removed

    def _remove_albums_for_root(self, root_name: str, state: AlbumState) -> int:
        prefix = f"{root_name.strip()} - "
        removed = 0
        photos_client = STATE.photos
        if photos_client is None:
            print("[WARN] Unable to delete albums; Synology session unavailable")
            return removed
        for album in list(state.existing_albums):
            if album.get("type") != "condition":
                continue
            album_name = album.get("name", "")
            if not album_name.startswith(prefix):
                continue
            album_id = album.get("id")
            if album_id is None:
                continue
            try:
                photos_client.delete_album(album_id)
                print(f"[INFO] Removed album '{album_name}' (id {album_id})")
                removed += 1
                self._unregister_album_cache(album, state)
            except Exception as exc:
                print(f"[ERROR] Failed to delete album '{album_name}': {exc}")
        return removed


__all__ = ["AlbumService", "AlbumState"]
