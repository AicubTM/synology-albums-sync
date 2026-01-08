"""Configuration loading and runtime state for the Synology Photos sync tool."""

from __future__ import annotations

import inspect
import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import pyotp
from dotenv import load_dotenv
from synology_api import exceptions, photos

try:
    import pwd
except ImportError:  # Windows compatibility
    pwd = None

class ConfigError(Exception):
    """Base exception for configuration issues."""


class MissingEnvError(ConfigError):
    """Raised when required environment variables are missing."""


class ConfigFileError(ConfigError):
    """Raised when the JSON config file is missing or invalid."""


load_dotenv()

CONFIG_ENV = os.getenv("SYNC_CONFIG_PATH", "sync_config.json")
CONFIG_PATH = CONFIG_ENV


def _resolve_config_path(path: str) -> str:
    """Resolve a config path: try as given, then relative to repo root when missing.

    If `path` is absolute, return it. For relative paths, prefer the cwd location
    if present, otherwise look for the file under the repository root (parent of
    the package directory). Returns the absolute candidate path (even if it does
    not exist) so callers can attempt to open it and handle missing files.
    """
    if os.path.isabs(path):
        return path
    # Candidate relative to current working directory
    cwd_candidate = os.path.abspath(path)
    if os.path.exists(cwd_candidate):
        return cwd_candidate
    # Candidate relative to repository root (package parent)
    pkg_dir = os.path.dirname(__file__)
    repo_root = os.path.abspath(os.path.join(pkg_dir, os.pardir))
    repo_candidate = os.path.join(repo_root, path)
    if os.path.exists(repo_candidate):
        return repo_candidate
    # Fallback to cwd candidate (may not exist) so caller sees the attempted path
    return cwd_candidate

RESERVED_NAMES: Set[str] = {
    "@eadir",
    "#snapshot",
    "@tmp",
    ".ds_store",
    "synologyphotos",
    "screenshots",
}

MEDIA_FILE_EXTENSIONS: Set[str] = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".arw",
    ".cr2",
    ".cr3",
    ".nef",
    ".dng",
    ".rw2",
    ".orf",
    ".raf",
    ".srw",
    ".pef",
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".avi",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".mts",
    ".m2ts",
    ".3gp",
    ".3g2",
    ".webm",
}

MEDIA_SCAN_MAX_DEPTH: Optional[int] = None
DEFAULT_RETRIES = 5
DEFAULT_RETRY_SLEEP_SECONDS = 5
INDEX_STATUS_POLL_SECONDS = 2


@dataclass
class SecuritySettings:
    ip: str
    port: int
    username: str
    password: str
    otp_secret: Optional[str]
    dsm_version: int
    totp: Optional[pyotp.TOTP] = None

    @classmethod
    def from_env(cls, json_section: Dict[str, object]) -> "SecuritySettings":
        ip = os.getenv("SYNOLOGY_IP")
        port = os.getenv("SYNOLOGY_PORT")
        username = os.getenv("SYNOLOGY_USERNAME")
        password = os.getenv("SYNOLOGY_PASSWORD")
        if not all([ip, port, username, password]):
            raise MissingEnvError("SYNOLOGY_* environment variables are required (IP, PORT, USERNAME, PASSWORD)")
        otp_secret = os.getenv("SYNOLOGY_OTP_SECRET") or None
        dsm_version = int(json_section.get("dsm_version", 7))
        totp = pyotp.TOTP(otp_secret) if otp_secret else None
        return cls(ip, int(port), username, password, otp_secret, dsm_version, totp)


def _build_virtual_root_path(shared_subdir: str) -> str:
    normalized = shared_subdir.strip("/\\")
    return f"/{normalized}" if normalized else "/"


@dataclass
class PathSettings:
    personal_homes_root: str
    personal_shared_subdir: str
    shared_photo_root: str
    root_mount_prefix: str
    personal_photos_root: str
    personal_link_root: str
    personal_link_virtual_root: str

    @classmethod
    def from_json(cls, data: Dict[str, object], username: str) -> "PathSettings":
        homes_root_raw = data.get("personal_homes_root")
        homes_root = (homes_root_raw or "/volume1/homes").rstrip("/\\") or "/volume1/homes"
        shared_subdir_raw = data.get("personal_shared_subdir")
        if shared_subdir_raw is None:
            shared_subdir = "photos-shared"
        else:
            shared_subdir = str(shared_subdir_raw).strip("/\\")
        shared_root = (data.get("shared_photo_root") or "/volume1/photo").rstrip("/\\") or "/volume1/photo"
        prefix = (data.get("root_mount_prefix") or "mount_").strip()
        personal_photos_root = f"{homes_root}/{username}/Photos"
        personal_link_root = os.path.normpath(os.path.join(personal_photos_root, shared_subdir or ""))
        personal_link_virtual_root = _build_virtual_root_path(shared_subdir)
        return cls(
            personal_homes_root=homes_root,
            personal_shared_subdir=shared_subdir,
            shared_photo_root=shared_root,
            root_mount_prefix=prefix,
            personal_photos_root=personal_photos_root,
            personal_link_root=personal_link_root,
            personal_link_virtual_root=personal_link_virtual_root,
        )


@dataclass
class MountSettings:
    enable_root_bind_mounts: bool
    mount_command: Optional[str]
    umount_command: Optional[str]

    @classmethod
    def from_json(cls, data: Dict[str, object]) -> "MountSettings":
        enable = bool(data.get("enable_root_bind_mounts", True))
        mount_cmd = shutil.which("mount") if enable and os.name != "nt" else None
        umount_cmd = shutil.which("umount") if enable and os.name != "nt" else None
        if enable and os.name == "nt":
            print("⚠️ Bind mounts are unavailable on Windows; disabling root bind mounts.")
            enable = False
        if enable and (not mount_cmd or not umount_cmd):
            print("⚠️ Unable to find required 'mount' or 'umount' binaries; disabling root bind mounts.")
            enable = False
        return cls(enable, mount_cmd, umount_cmd)


@dataclass
class MediaSettings:
    scan_max_depth: Optional[int]

    @classmethod
    def from_json(cls, data: Dict[str, object]) -> "MediaSettings":
        raw_value = data.get("scan_max_depth")
        if raw_value is None:
            return cls(scan_max_depth=None)
        try:
            depth = int(raw_value)
        except (TypeError, ValueError):
            print("⚠️ Invalid media.scan_max_depth; scanning full depth instead")
            return cls(scan_max_depth=None)
        if depth <= 0:
            return cls(scan_max_depth=None)
        return cls(scan_max_depth=depth)


@dataclass
class IndexingSettings:
    reindex_settle_seconds: int
    filter_wait_attempts: int
    filter_wait_delay: int
    force_reindex_on_start: bool
    reindex_after_link: bool
    personal_reindex_command: str

    @classmethod
    def from_json(cls, data: Dict[str, object]) -> "IndexingSettings":
        return cls(
            reindex_settle_seconds=int(data.get("reindex_settle_seconds", 10)),
            filter_wait_attempts=int(data.get("filter_wait_attempts", 12)),
            filter_wait_delay=int(data.get("filter_wait_delay", 5)),
            force_reindex_on_start=bool(data.get("force_reindex_on_start", False)),
            reindex_after_link=bool(data.get("reindex_after_link", True)),
            personal_reindex_command=str(data.get("personal_reindex_command", "")).strip(),
        )


def _coerce_list(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [token.strip() for token in value.split(",") if token.strip()]
    return []


@dataclass
class SharingSettings:
    managed_roots: List[str]
    root_share_with: Dict[str, List[str]]
    default_share_with: List[str]
    default_share_permission: str
    default_share_roles: List[str]
    enable_public_sharing: bool
    share_link_url_base: str
    target_roots: Dict[str, Dict[str, object]] = field(default_factory=dict)
    personal_target_roots: Dict[str, Dict[str, object]] = field(default_factory=dict)

    @classmethod
    def from_json(
        cls,
        data: Dict[str, object],
        shared_photo_root: str,
        root_mount_prefix: str,
        personal_photos_root: str,
    ) -> "SharingSettings":
        managed = _coerce_list(data.get("managed_roots"))
        root_share_with = {
            str(key): _coerce_list(value)
            for key, value in (data.get("root_share_with") or {}).items()
        }
        default_share_with = _coerce_list(data.get("default_share_with"))
        default_share_permission = str(data.get("default_share_permission", "view") or "view")
        default_share_roles = _coerce_list(data.get("default_share_roles"))
        enable_public_sharing = bool(data.get("enable_public_sharing", True))
        share_link_url_base = str(data.get("share_link_url_base", "") or "").rstrip("/")
        settings = cls(
            managed_roots=managed,
            root_share_with=root_share_with,
            default_share_with=default_share_with,
            default_share_permission=default_share_permission,
            default_share_roles=default_share_roles,
            enable_public_sharing=enable_public_sharing,
            share_link_url_base=share_link_url_base,
        )
        settings.target_roots = settings._build_target_roots(shared_photo_root, root_mount_prefix)
        personal_roots = data.get("personal_album_roots") or []
        settings.personal_target_roots = settings._build_personal_target_roots(personal_photos_root, personal_roots)
        return settings

    def _discover_root_names(self, base_path: str, root_mount_prefix: str) -> List[str]:
        prefix = root_mount_prefix.lower().strip()
        try:
            entries: List[str] = []
            with os.scandir(base_path) as iterator:
                for entry in iterator:
                    if not self._is_valid_root_name(entry.name):
                        continue
                    if prefix and entry.name.lower().startswith(prefix):
                        continue
                    if entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                        entries.append(entry.name)
            entries.sort(key=lambda value: value.lower())
            return entries
        except FileNotFoundError:
            print(f"⚠️ Managed root '{base_path}' does not exist; no folders discovered")
            return []

    @staticmethod
    def _is_valid_root_name(name: str) -> bool:
        normalized = (name or "").strip()
        if not normalized:
            return False
        lowered = normalized.lower()
        if lowered in RESERVED_NAMES:
            return False
        if normalized.startswith("."):
            return False
        return True

    def _build_target_roots(self, shared_photo_root: str, root_mount_prefix: str) -> Dict[str, Dict[str, object]]:
        root_names = self.managed_roots or self._discover_root_names(shared_photo_root, root_mount_prefix)
        targets: Dict[str, Dict[str, object]] = {}
        for root_name in root_names:
            label = root_name.strip()
            if not label:
                continue
            share_targets = list(self.root_share_with.get(label, [])) or list(self.default_share_with)
            entry: Dict[str, object] = {
                "share_with": share_targets,
                "permission": self.default_share_permission,
                "share_roles": list(self.default_share_roles),
            }
            targets[label] = entry
        if targets:
            joined = ", ".join(sorted(targets.keys(), key=str.lower))
            print(f"📁 Managing roots: {joined}")
        else:
            print("⚠️ No managed folders discovered. Set managed_roots or populate the personal shared directory.")
        return targets

    def _build_personal_target_roots(
        self,
        personal_photos_root: str,
        personal_roots: object,
    ) -> Dict[str, Dict[str, object]]:
        targets: Dict[str, Dict[str, object]] = {}
        entries = personal_roots or []
        if not isinstance(entries, list):
            print("⚠️ personal_album_roots must be a list of objects; ignoring invalid value")
            return targets
        for raw_entry in entries:
            if not isinstance(raw_entry, dict):
                continue
            raw_path = raw_entry.get("path") or raw_entry.get("relative_path")
            if raw_path is None:
                continue
            path_text = str(raw_path).strip()
            if not path_text:
                continue
            abs_path = os.path.normpath(path_text if os.path.isabs(path_text) else os.path.join(personal_photos_root, path_text))
            relative_token = os.path.relpath(abs_path, personal_photos_root).replace("\\", "/")
            if relative_token.startswith(".."):
                print(f"⚠️ Personal album root '{path_text}' is outside '{personal_photos_root}'; skipping")
                continue
            label = str(raw_entry.get("label") or relative_token.replace("/", " - ")).strip() or relative_token
            share_targets = _coerce_list(raw_entry.get("share_with")) or list(self.default_share_with)
            permission = str(raw_entry.get("permission") or raw_entry.get("share_permission") or self.default_share_permission)
            share_roles = _coerce_list(raw_entry.get("share_roles")) or list(self.default_share_roles)
            targets[label] = {
                "share_with": share_targets,
                "permission": permission,
                "share_roles": share_roles,
                "personal_path": abs_path,
                "relative_virtual_path": relative_token.strip("/"),
            }
        if targets:
            joined = ", ".join(sorted(targets.keys(), key=str.lower))
            print(f"📁 Managing personal roots: {joined}")
        return targets


def is_valid_root_name(name: str) -> bool:
    """Public helper that reuses the sharing rules for valid folder names."""

    return SharingSettings._is_valid_root_name(name)


@dataclass
class AppConfig:
    security: SecuritySettings
    paths: PathSettings
    mounts: MountSettings
    media: MediaSettings
    indexing: IndexingSettings
    sharing: SharingSettings
    cli_reindex_args: Optional[List[str]]
    share_album_supports_user_targets: bool
    share_album_role_parameter: Optional[str]


def _register_first_executable(paths: List[str]) -> Optional[str]:
    for candidate in paths:
        if not candidate:
            continue
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _detect_cli_reindex_args(config: AppConfig) -> Optional[List[str]]:
    if config.indexing.personal_reindex_command:
        return shlex.split(config.indexing.personal_reindex_command)

    userindex_bins = [
        shutil.which("synophoto_dsm_userindex"),
        "/usr/syno/bin/synophoto_dsm_userindex",
        "/usr/syno/sbin/synophoto_dsm_userindex",
        "/usr/syno/bin/synophoto_cms_userindex",
        "/var/packages/SynologyPhotos/target/usr/bin/synophoto_dsm_userindex",
        "/var/packages/SynologyPhotos/target/usr/lib/synophoto/bin/synophoto_dsm_userindex",
    ]
    userindex_path = _register_first_executable(userindex_bins)
    if userindex_path:
        return [userindex_path, "--user", config.security.username, "--rebuild"]

    index_tool_bins = [
        shutil.which("synofoto-bin-index-tool"),
        "/var/packages/SynologyPhotos/target/usr/bin/synofoto-bin-index-tool",
        "/var/packages/SynologyPhotos/target/usr/lib/synophoto/bin/synofoto-bin-index-tool",
    ]
    index_tool_path = _register_first_executable(index_tool_bins)
    if index_tool_path:
        target_path = config.paths.personal_photos_root
        return [index_tool_path, "-t", "basic", "-i", target_path]

    return None


@dataclass
class RuntimeState:
    photos: Optional[photos.Photos] = None
    current_user_id: Optional[int] = None
    session_ready: bool = False
    target_uid: Optional[int] = None
    target_gid: Optional[int] = None
    ownership_warning_emitted: bool = False
    folder_filter_cache: Dict[int, dict] = field(default_factory=dict)
    folder_filter_path_index: Dict[str, dict] = field(default_factory=dict)
    folder_filter_children: Dict[int, List[dict]] = field(default_factory=dict)
    folder_filter_sample_printed: bool = False
    link_changes_occurred: bool = False
    roots_awaiting_reindex: Set[str] = field(default_factory=set)
    targeted_reindex_requested: Set[str] = field(default_factory=set)
    roots_without_media_warned: Set[str] = field(default_factory=set)
    root_child_media_paths: Dict[str, List[str]] = field(default_factory=dict)
    root_child_media_states: Dict[str, str] = field(default_factory=dict)
    share_user_warning_emitted: bool = False
    share_role_warning_emitted: bool = False
    share_capabilities_printed: bool = False
    share_album_supports_user_targets: bool = True
    share_album_role_parameter: Optional[str] = None
    share_link_changes_flag: bool = False

    def clear_link_change_flag(self) -> None:
        self.link_changes_occurred = False

    def mark_link_change(self) -> None:
        self.link_changes_occurred = True


def _load_json_config(path: str = CONFIG_PATH) -> Dict[str, object]:
    resolved = _resolve_config_path(path)
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        # Provide helpful diagnostics: show where we looked
        tried = [os.path.abspath(path), resolved]
        tried_unique = []
        for p in tried:
            if p not in tried_unique:
                tried_unique.append(p)
        print(f"⚠️  Config file not found (tried): {', '.join(tried_unique)}; continuing with defaults")
        return {}
    except json.JSONDecodeError as exc:
        raise ConfigFileError(f"Invalid JSON in config file '{resolved}': {exc}") from exc


def load_app_config(path: str | None = None) -> AppConfig:
    data = _load_json_config(path or CONFIG_PATH)
    security = SecuritySettings.from_env(data.get("synology", {}))
    paths = PathSettings.from_json(data.get("paths", {}), security.username)
    mounts = MountSettings.from_json(data.get("mounts", {}))
    media = MediaSettings.from_json(data.get("media", {}))
    indexing = IndexingSettings.from_json(data.get("indexing", {}))
    sharing = SharingSettings.from_json(
        data.get("sharing", {}),
        paths.shared_photo_root,
        paths.root_mount_prefix,
        paths.personal_photos_root,
    )
    share_album_signature = inspect.signature(photos.Photos.share_album)
    supports_user_targets = "users" in share_album_signature.parameters
    share_role_parameter: Optional[str] = None
    for candidate in ("role", "roles"):
        if candidate in share_album_signature.parameters:
            share_role_parameter = candidate
            break
    app_config = AppConfig(
        security=security,
        paths=paths,
        mounts=mounts,
        media=media,
        indexing=indexing,
        sharing=sharing,
        cli_reindex_args=None,
        share_album_supports_user_targets=supports_user_targets,
        share_album_role_parameter=share_role_parameter,
    )
    app_config.cli_reindex_args = _detect_cli_reindex_args(app_config)
    global MEDIA_SCAN_MAX_DEPTH
    MEDIA_SCAN_MAX_DEPTH = media.scan_max_depth
    return app_config


def build_runtime_state(app_config: AppConfig) -> RuntimeState:
    return RuntimeState(
        share_album_supports_user_targets=app_config.share_album_supports_user_targets,
        share_album_role_parameter=app_config.share_album_role_parameter,
    )


APP_CONFIG = load_app_config()
RUNTIME_STATE = build_runtime_state(APP_CONFIG)


def lookup_user_ids(username: str) -> Tuple[Optional[int], Optional[int]]:
    if pwd is not None:
        try:
            entry = pwd.getpwnam(username)
            return entry.pw_uid, entry.pw_gid
        except KeyError:
            pass
    uid = os.getuid() if hasattr(os, "getuid") else None
    gid = os.getgid() if hasattr(os, "getgid") else None
    return uid, gid


target_uid, target_gid = lookup_user_ids(APP_CONFIG.security.username)
RUNTIME_STATE.target_uid = target_uid
RUNTIME_STATE.target_gid = target_gid

try:
    SYNO_EXCEPTION_BASE = exceptions.SynoBaseException  # type: ignore[attr-defined]
except AttributeError:  # older synology_api builds
    SYNO_EXCEPTION_BASE = None

SYN_LOGIN_EXCEPTION = SYNO_EXCEPTION_BASE or Exception

__all__ = [
    "APP_CONFIG",
    "RUNTIME_STATE",
    "AppConfig",
    "RuntimeState",
    "ConfigError",
    "MissingEnvError",
    "ConfigFileError",
    "MEDIA_FILE_EXTENSIONS",
    "MEDIA_SCAN_MAX_DEPTH",
    "MediaSettings",
    "RESERVED_NAMES",
    "SYNO_EXCEPTION_BASE",
    "SYN_LOGIN_EXCEPTION",
    "is_valid_root_name",
    "load_app_config",
    "build_runtime_state",
]
