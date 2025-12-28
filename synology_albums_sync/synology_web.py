"""Synology Photos web UI interaction fallbacks."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Dict, List, Optional, Set, Tuple

from synology_albums_sync import config


class SynologyWebSharing:
    """Helper that mirrors the Synology Photos web UI share flows."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        share_link_base: str = "",
        runtime_state=None,
        log_prefix: str = "[share]",
    ) -> None:
        self.host = host or "localhost"
        self.port = int(port or 5001)
        self.share_link_base = share_link_base.strip("/")
        self.state = runtime_state
        self.log_prefix = log_prefix
        self._shareable_identities: Dict[str, dict] = {}
        self._missing_share_identities: Set[str] = set()

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------
    def format_public_share_url(self, passphrase: str) -> str:
        """Construct a share URL that mirrors the Synology Photos UI."""
        if not passphrase:
            return ""
        if self.share_link_base:
            return f"{self.share_link_base}/{passphrase}"
        host_value = self.host
        needs_brackets = ":" in host_value and not host_value.startswith("[")
        if needs_brackets:
            host_value = f"[{host_value}]"
        port_value = str(self.port)
        scheme = "https" if port_value in {"443", "5001"} else "http"
        default_port = "443" if scheme == "https" else "80"
        port_fragment = "" if port_value == default_port else f":{port_value}"
        return f"{scheme}://{host_value}{port_fragment}/photo/share/{passphrase}"

    # ------------------------------------------------------------------
    # Identity resolution
    # ------------------------------------------------------------------
    def _photos_client(self):
        return getattr(self.state, "photos", None)

    def _ensure_shareable_identities(self) -> None:
        if self._shareable_identities or self._photos_client() is None:
            return
        photos_client = self._photos_client()
        info = (photos_client.photos_list or {}).get("SYNO.Foto.Sharing.Misc")
        if not info:
            print(f"{self.log_prefix} SYNO.Foto.Sharing.Misc API unavailable; cannot resolve share targets")
            return
        params = {
            "version": info.get("maxVersion", 1),
            "method": "list_user_group",
            "team_space_sharable_list": False,
        }
        try:
            response = photos_client.request_data("SYNO.Foto.Sharing.Misc", info["path"], params)
        except Exception as exc:
            print(f"{self.log_prefix} Unable to load shareable targets: {exc}")
            return
        entries = (response.get("data") or {}).get("list") or []
        cache: Dict[str, dict] = {}
        for entry in entries:
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            cache[name.lower()] = {
                "id": entry.get("id"),
                "type": entry.get("type", "user"),
                "name": name,
            }
        self._shareable_identities = cache
        if cache:
            print(f"{self.log_prefix} Cached {len(cache)} shareable targets")

    def build_permission_entries(self, targets: List[str], role_value: str) -> List[dict]:
        """Map usernames/groups to the payload Synology expects."""
        self._ensure_shareable_identities()
        permissions: List[dict] = []
        for raw_target in targets:
            key = (raw_target or "").strip().lower()
            if not key:
                continue
            identity = self._shareable_identities.get(key)
            if not identity:
                if key not in self._missing_share_identities:
                    print(f"{self.log_prefix} Target '{raw_target}' not found in user/group list")
                    self._missing_share_identities.add(key)
                continue
            permissions.append(
                {
                    "label": identity.get("name", raw_target) or raw_target,
                    "role": role_value,
                    "member": {
                        "id": identity.get("id"),
                        "type": identity.get("type", "user"),
                    },
                }
            )
        return permissions

    # ------------------------------------------------------------------
    # Invite updates
    # ------------------------------------------------------------------
    def sync_invite_permissions(
        self,
        api_name: str,
        api_path: str,
        version: int,
        passphrase: str,
        permissions: List[dict],
        target_label: str,
    ) -> None:
        """Replay UI invite updates so per-user permissions match policy."""
        photos_client = self._photos_client()
        if photos_client is None or not permissions:
            return
        for entry in permissions:
            member_block = deepcopy(entry.get("member") or {})
            if not member_block:
                continue
            permission_payload = {
                "role": entry.get("role"),
                "action": "update",
                "member": member_block,
            }
            update_payload = {
                "version": version,
                "method": "update",
                "passphrase": passphrase,
                "expiration": 0,
                "permission": json.dumps([permission_payload]),
            }
            label = entry.get("label") or member_block.get("type") or "target"
            try:
                invite_response = photos_client.request_data(api_name, api_path, update_payload)
            except Exception as exc:
                print(f"{self.log_prefix} Unable to sync invite '{label}' for '{target_label}': {exc}")
                continue
            if invite_response.get("success"):
                print(f"{self.log_prefix} Synced invite for '{target_label}' → {label}")
            else:
                code = (invite_response.get("error") or {}).get("code")
                print(f"{self.log_prefix} Synology rejected invite '{label}' for '{target_label}' (code={code})")

    # ------------------------------------------------------------------
    # Manual sharing entry point
    # ------------------------------------------------------------------
    def apply_private_sharing(
        self,
        target_label: str,
        share_with: List[str],
        permission: str,
        share_roles: List[str],
        *,
        api_name: str,
        policy: str,
        policy_kwargs: Dict[str, object],
    ) -> Tuple[bool, Optional[int]]:
        """Mirror Synology Photos web UI flows for per-user sharing."""
        photos_client = self._photos_client()
        if photos_client is None:
            print(f"{self.log_prefix} Unable to share '{target_label}' manually; session not initialized")
            return False, None
        share_info = (photos_client.photos_list or {}).get(api_name)
        if not share_info:
            print(f"{self.log_prefix} Manual sharing fallback unavailable; {api_name} missing")
            return False, None
        api_path = share_info.get("path")
        if not api_path:
            print(f"{self.log_prefix} {api_name} lacks a path entry; cannot share '{target_label}'")
            return False, None
        role_value = share_roles[0] if share_roles else permission or "view"
        permissions = self.build_permission_entries(share_with, role_value)
        if not permissions:
            print(f"{self.log_prefix} No resolvable share targets for '{target_label}'")
            return False, None
        permission_payloads = []
        for entry in permissions:
            member_block = entry.get("member")
            if not member_block:
                continue
            permission_payloads.append(
                {
                    "role": entry.get("role", role_value),
                    "member": member_block,
                }
            )
        version = share_info.get("maxVersion", 1)
        payload = {
            "version": version,
            "method": "set_shared",
            "policy": policy,
            **policy_kwargs,
            "enabled": True,
        }
        if permission_payloads:
            payload.update(
                {
                    "privacy_type": "private",
                    "permission": json.dumps(permission_payloads),
                    "enable_password": False,
                    "expiration": 0,
                }
            )
        try:
            shared_response = photos_client.request_data(api_name, api_path, payload)
        except Exception as exc:
            print(f"{self.log_prefix} Unable to prepare manual share for '{target_label}': {exc}")
            return False, None
        if not shared_response.get("success"):
            error_code = shared_response.get("error", {}).get("code")
            print(f"{self.log_prefix} Synology rejected share setup for '{target_label}' (code={error_code})")
            return False, error_code
        passphrase = (shared_response.get("data") or {}).get("passphrase")
        if not passphrase:
            print(f"{self.log_prefix} Manual share for '{target_label}' did not return a passphrase")
            return False, None
        share_url = self.format_public_share_url(passphrase)
        if share_url:
            print(f"{self.log_prefix} Enabled private link for '{target_label}' ({role_value}) → {share_url}")
        else:
            print(f"{self.log_prefix} Enabled private link for '{target_label}' ({role_value}); passphrase={passphrase}")
        self.sync_invite_permissions(api_name, api_path, version, passphrase, permissions, target_label)
        return True, None


DEFAULT_WEB_SHARING = SynologyWebSharing(
    host=config.APP_CONFIG.security.ip,
    port=config.APP_CONFIG.security.port,
    share_link_base=config.APP_CONFIG.sharing.share_link_url_base,
    runtime_state=config.RUNTIME_STATE,
)


def format_public_share_url(passphrase: str, sharing: Optional[SynologyWebSharing] = None) -> str:
    return (sharing or DEFAULT_WEB_SHARING).format_public_share_url(passphrase)


def build_permission_entries(targets: List[str], role_value: str, sharing: Optional[SynologyWebSharing] = None) -> List[dict]:
    return (sharing or DEFAULT_WEB_SHARING).build_permission_entries(targets, role_value)


def sync_invite_permissions(
    api_name: str,
    api_path: str,
    version: int,
    passphrase: str,
    permissions: List[dict],
    target_label: str,
    sharing: Optional[SynologyWebSharing] = None,
) -> None:
    (sharing or DEFAULT_WEB_SHARING).sync_invite_permissions(api_name, api_path, version, passphrase, permissions, target_label)


def apply_private_sharing(
    target_label: str,
    share_with: List[str],
    permission: str,
    share_roles: List[str],
    *,
    api_name: str,
    policy: str,
    policy_kwargs: Dict[str, object],
    sharing: Optional[SynologyWebSharing] = None,
) -> Tuple[bool, Optional[int]]:
    return (sharing or DEFAULT_WEB_SHARING).apply_private_sharing(
        target_label,
        share_with,
        permission,
        share_roles,
        api_name=api_name,
        policy=policy,
        policy_kwargs=policy_kwargs,
    )


__all__ = [
    "SynologyWebSharing",
    "DEFAULT_WEB_SHARING",
    "apply_private_sharing",
    "build_permission_entries",
    "format_public_share_url",
    "sync_invite_permissions",
]
