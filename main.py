"""Synology Photos Team Space synchronizer entrypoint."""

import argparse
import os
import sys
from typing import Dict, List, Optional, Tuple

from synology_albums_sync import config
from synology_albums_sync.albums import AlbumService
from synology_albums_sync.services import MediaService, MountService


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize Synology Photos Team Space folders into personal condition albums."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--mount",
        action="store_true",
        help="Mount managed Team Space folders only (no album changes).",
    )
    group.add_argument(
        "--create-albums",
        action="store_true",
        help="Create or refresh albums only (requires mounts to exist).",
    )
    group.add_argument(
        "--delete-albums",
        action="store_true",
        help="Delete managed albums only (keeps physical folders intact).",
    )
    group.add_argument(
        "--delete-album-by-name",
        metavar="NAME",
        help="Delete a single album by exact name (any album type).",
    )
    group.add_argument(
        "--list-albums",
        action="store_true",
        help="List albums (optionally restricted to a specific personal path).",
    )
    group.add_argument(
        "--unmount",
        action="store_true",
        help="Unmount managed folders without touching albums.",
    )
    group.add_argument(
        "--unmound",
        dest="unmount",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--unmount-all",
        action="store_true",
        help="Delete managed albums and unmount the folders.",
    )
    group.add_argument(
        "--unmap",
        dest="unmount_all",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--create-personal-albums",
        action="store_true",
        help="Create or refresh albums for configured personal roots only.",
    )
    group.add_argument(
        "--delete-personal-albums",
        action="store_true",
        help="Delete albums created for configured personal roots only.",
    )
    parser.add_argument(
        "--share-with",
        dest="share_with",
        help="Comma-separated Synology users/groups that override sharing targets for personal roots.",
    )
    parser.add_argument(
        "--roles",
        help="Comma-separated sharing roles that override configured roles for personal roots.",
    )
    parser.add_argument(
        "--permission",
        help="Override the sharing permission (view/download) for personal roots.",
    )
    parser.add_argument(
        "--max-depth",
        dest="max_depth",
        type=int,
        help="Override the maximum folder depth scanned for personal roots (<=0 scans full depth).",
    )
    parser.add_argument(
        "--path",
        help="Absolute path (or path relative to your personal Photos directory) for ad-hoc personal album operations.",
    )
    parser.add_argument(
        "--label-prefix",
        dest="label_prefix",
        help="Custom label prefix used for albums derived from an ad-hoc personal path.",
    )
    return parser.parse_args()


def _parse_csv_arg(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    entries = [token.strip() for token in value.split(",") if token.strip()]
    return entries


def _build_personal_entry_from_path(path: str, *, label_override: Optional[str] = None) -> Tuple[str, Dict[str, object]]:
    app_config = config.APP_CONFIG
    personal_root = os.path.abspath(app_config.paths.personal_photos_root)
    candidate = path if os.path.isabs(path) else os.path.join(personal_root, path)
    candidate = os.path.abspath(candidate)
    try:
        common_prefix = os.path.commonpath([candidate, personal_root])
    except ValueError as exc:
        raise ValueError(
            f"Personal path '{candidate}' must live on the same volume as '{personal_root}'"
        ) from exc
    if common_prefix != personal_root:
        raise ValueError(f"Personal path '{candidate}' must reside under '{personal_root}'")
    relative_token = os.path.relpath(candidate, personal_root).replace("\\", "/").strip("/")
    if not relative_token or relative_token == ".":
        raise ValueError("Personal path must point to a subfolder inside the personal Photos directory")
    if label_override is not None:
        label = label_override
    else:
        label = relative_token.replace("/", " - ")
    share_conf: Dict[str, object] = {
        "share_with": list(app_config.sharing.default_share_with),
        "permission": app_config.sharing.default_share_permission,
        "share_roles": list(app_config.sharing.default_share_roles),
        "personal_path": candidate,
        "relative_virtual_path": relative_token,
    }
    return label, share_conf


def main() -> None:
    args = parse_cli_args()

    media_service = MediaService()
    mount_service = MountService(media_service)
    album_service = AlbumService(media_service, mount_service=mount_service)
    override_flags_used = any(
        [
            args.share_with,
            args.roles,
            args.permission,
            args.max_depth is not None,
        ]
    )
    path_flags_used = bool(args.path or args.label_prefix is not None)
    if override_flags_used and not args.create_personal_albums:
        print("❌ Personal override flags require --create-personal-albums")
        return
    if path_flags_used and not (
        args.create_personal_albums or args.delete_personal_albums or args.list_albums
    ):
        print("❌ --path/--label-prefix require --create-personal-albums, --delete-personal-albums, or --list-albums")
        return
    if args.label_prefix is not None and not args.path:
        print("❌ --label-prefix requires --path")
        return
    personal_share_with = None
    personal_roles = None
    personal_permission = None
    personal_max_depth = None
    explicit_roots = None
    explicit_labels = None
    label_override = args.label_prefix.strip() if args.label_prefix is not None else None
    if args.path:
        try:
            label, entry = _build_personal_entry_from_path(args.path, label_override=label_override)
        except ValueError as exc:
            print(f"❌ {exc}")
            return
        explicit_roots = [(label, entry)]
        explicit_labels = [label]
    if args.create_personal_albums:
        personal_share_with = _parse_csv_arg(args.share_with)
        personal_roles = _parse_csv_arg(args.roles)
        personal_permission = args.permission.strip() if args.permission else None
        personal_max_depth = args.max_depth

    has_team_roots = bool(config.APP_CONFIG.sharing.target_roots)
    has_personal_roots = bool(config.APP_CONFIG.sharing.personal_target_roots)
    has_explicit_personal = bool(explicit_roots)

    if args.create_personal_albums or args.delete_personal_albums:
        if not (has_personal_roots or has_explicit_personal):
            print("❌ No personal roots configured; provide --path or add personal_album_roots in sync_config.json")
            return
    else:
        if not (has_team_roots or has_personal_roots):
            print("❌ No managed roots defined; update sync_config.json before running")
            return

    if args.mount:
        mount_service.ensure_mounts(False, label="mount command")
        return

    if args.delete_album_by_name:
        album_service.delete_album_by_name(args.delete_album_by_name)
        return

    if args.list_albums:
        album_service.list_albums(target_path=args.path)
        return

    if args.delete_albums:
        album_service.delete_albums_only()
        return

    if args.delete_personal_albums:
        album_service.delete_personal_albums_only(labels=explicit_labels, explicit_roots=explicit_roots)
        return

    if args.create_personal_albums:
        album_service.run_personal_roots(
            share_with_override=personal_share_with,
            share_roles_override=personal_roles,
            permission_override=personal_permission,
            max_depth_override=personal_max_depth,
            explicit_roots=explicit_roots,
        )
        return

    if args.create_albums:
        mount_service.reindex_and_wait("album creation prep")
        album_service.run_sync(manage_mounts=False, allow_defer_on_mount=False)
        return

    if args.unmount:
        mount_service.unmount_roots_only()
        return

    if args.unmount_all:
        album_service.unmap_all_roots_and_albums()
        return

    mount_service.ensure_mounts(True, label="pre-sync mount")
    album_service.run_sync(manage_mounts=False, allow_defer_on_mount=False)


if __name__ == "__main__":
    main()
