"""Synology Photos API helpers and wrappers."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Dict, List, Optional, Set, Tuple

from synology_api import photos

from synology_albums_sync import config
from synology_albums_sync.paths import normalize_personal_path, normalize_team_label
from synology_albums_sync.synology_web import DEFAULT_WEB_SHARING, SynologyWebSharing


def collect_direct_team_child_names(root_label: str, team_children: List[dict]) -> Set[str]:
    names: Set[str] = set()
    normalized_root = normalize_team_label(root_label)
    for entry in team_children:
        raw_name = str(entry.get("name", "")).strip()
        if not raw_name:
            continue
        cleaned = raw_name.lstrip("/")
        if not cleaned:
            continue
        parts = cleaned.split("/", 1)
        entry_root = normalize_team_label(parts[0]) if parts else ""
        if entry_root and entry_root != normalized_root:
            continue
        remainder = parts[1] if len(parts) > 1 else ""
        if not remainder:
            continue
        child_token = remainder.split("/", 1)[0].strip()
        if child_token:
            names.add(child_token.lower())
    return names


class SynologyPhotosAPI:
    """Reusable Synology Photos helper that hides DSM-specific quirks."""

    def __init__(
        self,
        *,
        app_config,
        runtime_state,
        web_sharing: Optional[SynologyWebSharing] = None,
        index_status_poll_seconds: Optional[int] = None,
        default_retries: Optional[int] = None,
        retry_sleep_seconds: Optional[int] = None,
        syn_exception_base: Optional[type] = None,
        syn_login_exception: Optional[type] = None,
    ) -> None:
        self.app_config = app_config
        self.state = runtime_state
        self.web_sharing = web_sharing or SynologyWebSharing(
            host=app_config.security.ip,
            port=app_config.security.port,
            share_link_base=app_config.sharing.share_link_url_base,
            runtime_state=runtime_state,
        )
        self.index_status_poll_seconds = index_status_poll_seconds or getattr(config, "INDEX_STATUS_POLL_SECONDS", 2)
        self.default_retries = default_retries or getattr(config, "DEFAULT_RETRIES", 5)
        self.retry_sleep_seconds = retry_sleep_seconds or getattr(config, "DEFAULT_RETRY_SLEEP_SECONDS", 5)
        self.syn_exception_base = syn_exception_base or getattr(config, "SYNO_EXCEPTION_BASE", None)
        self.syn_login_exception = syn_login_exception or getattr(config, "SYN_LOGIN_EXCEPTION", Exception)

    # ------------------------------------------------------------------
    # Error helpers
    # ------------------------------------------------------------------
    def describe_synology_error(self, exc: Exception) -> str:
        if self.syn_exception_base and isinstance(exc, self.syn_exception_base):
            code = getattr(exc, "error_code", None)
            base_message = getattr(exc, "error_message", "") or str(exc)
            detail = f"error code {code}" if code is not None else "Synology API error"
            if base_message:
                detail += f": {base_message}"
            return detail
        return str(exc)

    # ------------------------------------------------------------------
    # Index helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _interpret_index_state(state: object) -> Optional[bool]:
        if not isinstance(state, dict):
            return None
        for key in ("is_running", "running", "busy"):
            if key not in state:
                continue
            value = state[key]
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"true", "1", "running", "busy"}:
                    return True
                if lowered in {"false", "0", "idle", "done", "finish", "finished"}:
                    return False
        status_value = state.get("status") or state.get("state")
        if isinstance(status_value, str):
            lowered = status_value.strip().lower()
            if lowered in {"idle", "ready", "finish", "finished", "done", "complete"}:
                return False
            if lowered in {"busy", "running", "processing", "working"}:
                return True
        return None

    def _fetch_index_state(self) -> Optional[dict]:
        photos_client = self.state.photos
        if photos_client is None:
            return None
        info = (photos_client.photos_list or {}).get("SYNO.Foto.Index")
        if not info:
            return None
        try:
            response = photos_client.request_data(
                "SYNO.Foto.Index",
                info["path"],
                {"version": info["maxVersion"], "method": "get"},
            )
        except Exception as exc:
            detail = self.describe_synology_error(exc)
            print(f"⚠️ Unable to query SYNO.Foto.Index status: {detail}")
            return None
        return response.get("data") if isinstance(response, dict) else None

    def _wait_for_index_idle(self, timeout_seconds: int) -> Tuple[bool, float]:
        if timeout_seconds <= 0:
            return False, 0.0
        photos_client = self.state.photos
        if photos_client is None:
            return False, 0.0
        info = (photos_client.photos_list or {}).get("SYNO.Foto.Index")
        if not info:
            return False, 0.0
        start = time.monotonic()
        poll_delay = max(1, min(10, self.index_status_poll_seconds))
        interpreted: Optional[bool] = None
        while time.monotonic() - start < timeout_seconds:
            state = self._fetch_index_state()
            interpreted = self._interpret_index_state(state)
            if interpreted is None:
                break
            if not interpreted:
                elapsed = time.monotonic() - start
                print(f"✅ SYNO.Foto.Index reports idle after {elapsed:.1f}s")
                return True, elapsed
            time.sleep(poll_delay)
        elapsed = time.monotonic() - start
        if interpreted is True:
            print(f"⏳ SYNO.Foto.Index still busy after {elapsed:.1f}s; continuing anyway")
            return True, elapsed
        return False, elapsed

    # ------------------------------------------------------------------
    # Reindex helpers
    # ------------------------------------------------------------------
    def _build_targeted_reindex_args(self, target_path: str) -> Optional[List[str]]:
        cli_args = self.app_config.cli_reindex_args
        if not cli_args:
            return None
        binary = cli_args[0]
        if not binary:
            return None
        name = os.path.basename(binary)
        normalized_path = os.path.abspath(target_path)
        if "synophoto_dsm_userindex" in name:
            return [binary, "--user", self.app_config.security.username, "--path", normalized_path]
        if "synofoto-bin-index-tool" in name:
            return [binary, "-t", "basic", "-i", normalized_path]
        return None

    def request_targeted_reindex(self, target_path: str) -> bool:
        normalized = os.path.abspath(target_path)
        if not normalized or normalized in self.state.targeted_reindex_requested:
            return False
        args = self._build_targeted_reindex_args(normalized)
        if not args:
            return False
        try:
            subprocess.run(args, check=True, capture_output=True, text=True)
            self.state.targeted_reindex_requested.add(normalized)
            print(f"♻️ Requested targeted reindex for '{normalized}'")
            return True
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else ""
            stdout = exc.stdout.strip() if exc.stdout else ""
            detail = stderr or stdout or str(exc)
            print(f"⚠️ Failed targeted reindex for '{normalized}': {detail}")
        except FileNotFoundError:
            print("⚠️ Targeted reindex command unavailable")
        except PermissionError as exc:
            print(f"⚠️ Permission denied while requesting targeted reindex for '{normalized}': {exc}")
        return False

    def _trigger_personal_reindex_api(self) -> bool:
        photos_client = self.state.photos
        if photos_client is None:
            print("⚠️ SYNO.Foto.Index API unavailable; skipping API reindex")
            return False
        info = (photos_client.photos_list or {}).get("SYNO.Foto.Index")
        if not info:
            print("⚠️ SYNO.Foto.Index API unavailable; skipping API reindex")
            return False
        try:
            photos_client.request_data(
                "SYNO.Foto.Index",
                info["path"],
                {"version": info["maxVersion"], "method": "reindex", "type": "basic"},
            )
            print("♻️ Triggered personal-space reindex via API")
            return True
        except Exception as exc:
            detail = self.describe_synology_error(exc)
            print(f"❌ Failed to trigger reindex via API: {detail}")
            return False

    def _trigger_personal_reindex_cli(self) -> bool:
        cli_args = self.app_config.cli_reindex_args
        if not cli_args:
            return False
        try:
            completed = subprocess.run(
                cli_args,
                check=True,
                capture_output=True,
                text=True,
            )
            stdout = completed.stdout.strip()
            if stdout:
                print(f"♻️ CLI reindex output: {stdout}")
            print("♻️ Triggered personal-space reindex via CLI helper")
            return True
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else ""
            stdout = exc.stdout.strip() if exc.stdout else ""
            detail = stderr or stdout or str(exc)
            print(f"❌ CLI reindex command failed ({' '.join(cli_args)}): {detail}")
            return False
        except FileNotFoundError:
            print("⚠️ CLI reindex command missing when attempting fallback")
        except PermissionError as exc:
            print(f"⚠️ Permission denied when invoking CLI reindex command: {exc}")
        return False

    def _post_reindex_settle(self) -> None:
        settle_seconds = self.app_config.indexing.reindex_settle_seconds
        if settle_seconds <= 0:
            return
        handled, elapsed = self._wait_for_index_idle(settle_seconds)
        if handled:
            return
        remaining = settle_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def trigger_personal_reindex(self) -> bool:
        if not self.app_config.indexing.reindex_after_link:
            return False
        if self._trigger_personal_reindex_api():
            self._post_reindex_settle()
            return True
        if self._trigger_personal_reindex_cli():
            self._post_reindex_settle()
            return True
        return False

    # ------------------------------------------------------------------
    # Folder filter helpers
    # ------------------------------------------------------------------
    def debug_dump_nearby_folder_filters(self, target_path: str, limit: int = 10) -> None:
        if not self.state.folder_filter_cache:
            print("🕵️ Folder cache empty; cannot debug target path")
            return
        normalized_target = normalize_personal_path(target_path).lower()
        print(f"🕵️ Debug: no exact folder_filter match for '{normalized_target}'")
        matches: List[Tuple[str, int]] = []
        for entry in self.state.folder_filter_cache.values():
            name = normalize_personal_path(entry.get("name", "")).lower()
            if normalized_target.strip("/") in name:
                try:
                    matches.append((name, int(entry.get("id"))))
                except (TypeError, ValueError):
                    matches.append((name, -1))
        matches.sort()
        if not matches:
            print("🕵️ Debug: no partial matches either; check personal_homes_root/personal_shared_subdir settings")
            return
        print(f"🕵️ Debug: showing up to {limit} folder_filter entries containing target token")
        for name, entry_id in matches[:limit]:
            print(f"   • {name} (id {entry_id})")

    @staticmethod
    def _register_folder_filter_entry(
        cache: Dict[int, dict],
        path_index: Dict[str, dict],
        children_map: Dict[int, List[dict]],
        entry: dict,
    ) -> None:
        entry_id: Optional[int] = None
        for key in ("id", "folder_id"):
            raw_value = entry.get(key)
            if raw_value is None:
                continue
            try:
                entry_id = int(raw_value)
                cache[entry_id] = entry
                break
            except (TypeError, ValueError):
                continue
        normalized_path = normalize_personal_path(entry.get("name", "")).lower()
        if normalized_path:
            path_index[normalized_path] = entry
        parent_raw = entry.get("parent")
        try:
            parent_id = int(parent_raw)
        except (TypeError, ValueError, AttributeError):
            parent_id = None
        if parent_id is not None:
            children_map.setdefault(parent_id, []).append(entry)

    def load_folder_filters(self) -> None:
        cache: Dict[int, dict] = {}
        path_index: Dict[str, dict] = {}
        children_map: Dict[int, List[dict]] = {}
        photos_client = self.state.photos
        info = (photos_client.photos_list or {}).get("SYNO.Foto.Search.Filter") if photos_client else None
        if not info:
            raise RuntimeError("SYNO.Foto.Search.Filter API unavailable")
        req_param = {"version": info["maxVersion"], "method": "list"}
        try:
            response = photos_client.request_data("SYNO.Foto.Search.Filter", info["path"], req_param)
            folder_entries = response.get("data", {}).get("folder_filter", []) or []
            for entry in folder_entries:
                self._register_folder_filter_entry(cache, path_index, children_map, entry)
                if not self.state.folder_filter_sample_printed:
                    keys = ", ".join(sorted(entry.keys()))
                    print(f"🧪 Sample folder_filter keys: {keys}")
                    self.state.folder_filter_sample_printed = True
        except Exception as exc:
            print(f"❌ Unable to load personal folder filters: {exc}")
            raise
        for child_list in children_map.values():
            child_list.sort(key=lambda item: item.get("name", "").lower())
        self.state.folder_filter_cache = cache
        self.state.folder_filter_path_index = path_index
        self.state.folder_filter_children = children_map
        print(f"📂 Cached {len(cache)} personal folder filter entries")

    def wait_for_folder_entry(self, path: str) -> Optional[dict]:
        normalized = normalize_personal_path(path)
        max_attempts = self.app_config.indexing.filter_wait_attempts
        delay_seconds = self.app_config.indexing.filter_wait_delay
        for attempt in range(1, max_attempts + 1):
            entry = self.state.folder_filter_path_index.get(normalized.lower())
            if entry:
                return entry
            if attempt < max_attempts:
                print(f"⏳ Waiting for folder '{normalized}' to be indexed ({attempt}/{max_attempts})")
                time.sleep(delay_seconds)
                self.load_folder_filters()
        self.debug_dump_nearby_folder_filters(normalized)
        return None

    def wait_for_paths_indexed(self, paths: List[str], *, label: str) -> Set[str]:
        normalized_map: Dict[str, str] = {}
        for path in paths:
            if not path:
                continue
            normalized = normalize_personal_path(path)
            normalized_map[normalized.lower()] = normalized
        pending: Set[str] = set(normalized_map.keys())
        if not pending:
            return set()
        max_attempts = self.app_config.indexing.filter_wait_attempts
        delay_seconds = self.app_config.indexing.filter_wait_delay
        for attempt in range(1, max_attempts + 1):
            self.load_folder_filters()
            resolved: Set[str] = set()
            for token in list(pending):
                normalized_path = normalized_map[token]
                if self.state.folder_filter_path_index.get(normalized_path.lower()):
                    resolved.add(token)
            pending.difference_update(resolved)
            if not pending:
                print(f"✅ DSM indexing complete for {label}")
                return set()
            if attempt < max_attempts:
                preview_values = [normalized_map.get(token, token) for token in sorted(pending)[:3]]
                preview = ", ".join(preview_values)
                extra = "" if len(pending) <= 3 else f" +{len(pending) - 3} more"
                print(
                    f"⏳ Waiting for DSM indexing to finish ({label}) ({attempt}/{max_attempts}); pending: {preview}{extra}"
                )
                time.sleep(delay_seconds)
        if pending:
            preview_values = [normalized_map.get(token, token) for token in sorted(pending)[:5]]
            preview = ", ".join(preview_values)
            extra = "" if len(pending) <= 5 else f" +{len(pending) - 5} more"
            print(f"⚠️ Timed out waiting for DSM indexing to finish for {label}; still pending: {preview}{extra}")
            return {normalized_map.get(token, token) for token in pending}
        return set()

    def log_unindexed_paths(self, label: str, pending_paths: Set[str]) -> None:
        if not pending_paths:
            return
        sorted_paths = sorted(pending_paths)
        preview = ", ".join(sorted_paths[:10])
        extra = "" if len(sorted_paths) <= 10 else f" +{len(sorted_paths) - 10} more"
        print(f"🕵️ DSM still missing folder_filter entries after {label}: {preview}{extra}")

    # ------------------------------------------------------------------
    # Sharing capability helpers
    # ------------------------------------------------------------------
    def describe_sharing_capabilities(self, reason: Optional[str] = None) -> None:
        if self.state.share_capabilities_printed or self.state.photos is None:
            return
        share_entries: List[Tuple[str, dict]] = []
        for name, info in (self.state.photos.photos_list or {}).items():
            if "Sharing" not in name or not isinstance(info, dict):
                continue
            share_entries.append((name, info))
        if not share_entries:
            print("🕵️ No sharing APIs discovered in photos_list; per-user sharing may be unsupported on this DSM build")
        else:
            header = "🔍 Available sharing APIs"
            if reason:
                header += f" ({reason})"
            print(header)
            for name, info in sorted(share_entries, key=lambda item: item[0]):
                max_version = info.get("maxVersion")
                min_version = info.get("minVersion")
                path = info.get("path")
                print(f"   • {name} (path={path}, min={min_version}, max={max_version})")
                if self.app_config.sharing.enable_public_sharing and name == "SYNO.Foto.PublicSharing":
                    required_version = 1
                    if info.get("maxVersion", 0) < required_version:
                        print(
                            "     ↳ ⚠️ PUBLIC sharing fallback requires version 1; available max is "
                            f"{info.get('maxVersion')}"
                        )
        self.state.share_capabilities_printed = True

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------
    def ensure_photos_session(self) -> None:
        if self.state.session_ready:
            return
        for attempt in range(1, self.default_retries + 1):
            try:
                photos_client = photos.Photos(
                    self.app_config.security.ip,
                    self.app_config.security.port,
                    self.app_config.security.username,
                    self.app_config.security.password,
                    secure=False,
                    cert_verify=False,
                    dsm_version=self.app_config.security.dsm_version,
                    debug=True,
                    otp_code=(self.app_config.security.totp.now() if self.app_config.security.totp else None),
                )
                self.state.photos = photos_client
                print(
                    f"✅ Logged in as {self.app_config.security.username} at {self.app_config.security.ip}:{self.app_config.security.port}"
                )
                try:
                    user_info = photos_client.get_userinfo()
                    self.state.current_user_id = int(user_info["data"]["id"])
                    print(f"👤 Using user id {self.state.current_user_id} for album conditions")
                    self.load_folder_filters()
                    if not self.state.share_album_supports_user_targets:
                        self.describe_sharing_capabilities("synology_api client lacks per-user share parameters")
                    self.state.session_ready = True
                    return
                except Exception as user_exc:
                    print(f"❌ Failed to bootstrap session metadata: {user_exc}")
                    raise
            except self.syn_login_exception as exc:
                print(f"Login attempt {attempt} failed: {exc}")
                if attempt == self.default_retries:
                    print("❌ Unable to log in, stopping.")
                    raise
                time.sleep(self.retry_sleep_seconds)

    def find_team_root_entry(self, target_label: str) -> Optional[dict]:
        if self.state.photos is None:
            return None
        response = self.state.photos.list_teams_folders(0)
        data = response.get("data", {}) if isinstance(response, dict) else {}
        for entry in data.get("list", []):
            if normalize_team_label(entry.get("name", "")) == normalize_team_label(target_label):
                return entry
        return None

    def list_team_child_folders(self, root_id: int) -> List[dict]:
        if self.state.photos is None or root_id is None:
            return []
        response = self.state.photos.list_teams_folders(root_id)
        if not isinstance(response, dict):
            return []
        return response.get("data", {}).get("list", []) or []

    def fetch_existing_albums(self, limit: int = 5000) -> List[dict]:
        if self.state.photos is None:
            return []
        response = self.state.photos.list_albums(limit=limit)
        if not isinstance(response, dict):
            raise ValueError("Invalid response for list_albums")
        albums = response.get("data", {}).get("list", [])
        print(f"📚 Found {len(albums)} existing albums")
        return albums

    # ------------------------------------------------------------------
    # Sharing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_role(role_value: str) -> str:
        mapping = {
            "downloader": "download",
            "download": "download",
            "viewer": "view",
            "view": "view",
            "manager": "manager",
            "editor": "manager",
        }
        normalized = (role_value or "").strip()
        if not normalized:
            return "view"
        lowered = normalized.lower()
        return mapping.get(lowered, normalized)

    def share_album(
        self,
        album_id: int,
        album_name: str,
        share_with: List[str],
        permission: str,
        share_roles: List[str],
    ) -> None:
        normalized_permission = self._normalize_role(permission)
        if not share_with:
            print(f"ℹ️ No share target for '{album_name}', leaving private")
            return
        if share_with and not self.state.share_album_supports_user_targets:
            if not self.state.share_user_warning_emitted:
                print(
                    "⚠️ Installed synology_api version does not accept per-user share targets; trying alternative share mechanism"
                )
                self.state.share_user_warning_emitted = True
            if self.app_config.sharing.enable_public_sharing:
                success, _ = self.web_sharing.apply_private_sharing(
                    album_name,
                    share_with,
                    normalized_permission,
                    share_roles,
                    api_name="SYNO.Foto.Sharing.Passphrase",
                    policy="album",
                    policy_kwargs={"album_id": album_id},
                )
                if success:
                    return
                print(f"ℹ️ '{album_name}' left private (manual sharing fallback failed)")
            else:
                print(f"ℹ️ '{album_name}' left private (no user share API support)")
            return
        kwargs = {"users": share_with, "permission": normalized_permission}
        role_note = ""
        normalized_roles: List[str] = []
        for role in share_roles:
            role_text = self._normalize_role(role)
            if role_text and role_text not in normalized_roles:
                normalized_roles.append(role_text)
        role_parameter = self.state.share_album_role_parameter
        if normalized_roles and role_parameter:
            if role_parameter == "roles":
                kwargs["roles"] = normalized_roles
                role_note = f", roles={normalized_roles}"
            else:
                kwargs["role"] = normalized_roles[0]
                role_note = f", role={normalized_roles[0]}"
        elif normalized_roles and not self.state.share_role_warning_emitted:
            print("⚠️ Installed synology_api version does not accept per-user role targets; requested share roles will be ignored")
            self.state.share_role_warning_emitted = True
        try:
            if self.state.photos is None:
                print(f"⚠️ Unable to share '{album_name}'; session not ready")
                return
            self.state.photos.share_album(album_id, **kwargs)
            print(f"🔗 Shared '{album_name}' with {share_with} (permission={permission}{role_note})")
        except Exception as exc:
            print(f"❌ Failed to share '{album_name}': {exc}")


DEFAULT_PHOTOS_API = SynologyPhotosAPI(
    app_config=config.APP_CONFIG,
    runtime_state=config.RUNTIME_STATE,
    web_sharing=DEFAULT_WEB_SHARING,
    index_status_poll_seconds=getattr(config, "INDEX_STATUS_POLL_SECONDS", 2),
    default_retries=getattr(config, "DEFAULT_RETRIES", 5),
    retry_sleep_seconds=getattr(config, "DEFAULT_RETRY_SLEEP_SECONDS", 5),
)


def describe_synology_error(exc: Exception, api: Optional[SynologyPhotosAPI] = None) -> str:
    return (api or DEFAULT_PHOTOS_API).describe_synology_error(exc)


def request_targeted_reindex(target_path: str, api: Optional[SynologyPhotosAPI] = None) -> bool:
    return (api or DEFAULT_PHOTOS_API).request_targeted_reindex(target_path)


def trigger_personal_reindex(api: Optional[SynologyPhotosAPI] = None) -> bool:
    return (api or DEFAULT_PHOTOS_API).trigger_personal_reindex()


def debug_dump_nearby_folder_filters(target_path: str, limit: int = 10, api: Optional[SynologyPhotosAPI] = None) -> None:
    (api or DEFAULT_PHOTOS_API).debug_dump_nearby_folder_filters(target_path, limit=limit)


def load_folder_filters(api: Optional[SynologyPhotosAPI] = None) -> None:
    (api or DEFAULT_PHOTOS_API).load_folder_filters()


def wait_for_folder_entry(path: str, api: Optional[SynologyPhotosAPI] = None) -> Optional[dict]:
    return (api or DEFAULT_PHOTOS_API).wait_for_folder_entry(path)


def wait_for_paths_indexed(paths: List[str], *, label: str, api: Optional[SynologyPhotosAPI] = None) -> Set[str]:
    return (api or DEFAULT_PHOTOS_API).wait_for_paths_indexed(paths, label=label)


def log_unindexed_paths(label: str, pending_paths: Set[str], api: Optional[SynologyPhotosAPI] = None) -> None:
    (api or DEFAULT_PHOTOS_API).log_unindexed_paths(label, pending_paths)


def describe_sharing_capabilities(reason: Optional[str] = None, api: Optional[SynologyPhotosAPI] = None) -> None:
    (api or DEFAULT_PHOTOS_API).describe_sharing_capabilities(reason)


def ensure_photos_session(api: Optional[SynologyPhotosAPI] = None) -> None:
    (api or DEFAULT_PHOTOS_API).ensure_photos_session()


def find_team_root_entry(target_label: str, api: Optional[SynologyPhotosAPI] = None) -> Optional[dict]:
    return (api or DEFAULT_PHOTOS_API).find_team_root_entry(target_label)


def list_team_child_folders(root_id: int, api: Optional[SynologyPhotosAPI] = None) -> List[dict]:
    return (api or DEFAULT_PHOTOS_API).list_team_child_folders(root_id)


def fetch_existing_albums(limit: int = 5000, api: Optional[SynologyPhotosAPI] = None) -> List[dict]:
    return (api or DEFAULT_PHOTOS_API).fetch_existing_albums(limit=limit)


def share_album(
    album_id: int,
    album_name: str,
    share_with: List[str],
    permission: str,
    share_roles: List[str],
    api: Optional[SynologyPhotosAPI] = None,
) -> None:
    (api or DEFAULT_PHOTOS_API).share_album(album_id, album_name, share_with, permission, share_roles)


__all__ = [
    "SynologyPhotosAPI",
    "DEFAULT_PHOTOS_API",
    "collect_direct_team_child_names",
    "debug_dump_nearby_folder_filters",
    "describe_sharing_capabilities",
    "describe_synology_error",
    "ensure_photos_session",
    "fetch_existing_albums",
    "find_team_root_entry",
    "list_team_child_folders",
    "load_folder_filters",
    "log_unindexed_paths",
    "request_targeted_reindex",
    "share_album",
    "trigger_personal_reindex",
    "wait_for_folder_entry",
    "wait_for_paths_indexed",
]
