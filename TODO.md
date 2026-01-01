# Synology Albums Sync TODOs

## Change Log
- 2026-01-01: Added `--list-albums` (with optional `--path` scoping) and `--delete-album-by-name`, plus README docs and path filtering fixes.
- 2026-01-01: Clarified README examples to show when share/role flags are optional vs. pulled from `sync_config.json` for both Team Space and personal flows.
- 2025-12-29: Rebranded the project/repo to **Synology Albums Sync** so downstream references and DSM task scripts use the same concise name.
- 2025-12-28: Added project-layout docs in README plus `docs/api-usage.md` to document Synology API/Web helper reuse.
- 2025-12-28: Extended `--delete-personal-albums` so it can target ad-hoc paths/labels just like the create flow, keeping CLI overrides symmetric.
- 2025-12-27: Shortened the personal override CLI flags (`--share-with`, `--roles`, `--permission`, `--max-depth`, `--path`) and added `--label-prefix` so ad-hoc personal runs can control album naming without editing the config or accepting auto-derived labels.
- 2025-12-28: Updated `--label-prefix` handling so empty strings are allowed (dropping the prefix and naming albums strictly after the child folder) and documented the behavior.
- 2025-12-27: Added CLI switches to create/delete personal albums with overrideable sharing targets/roles/permissions plus configurable media.scan_max_depth (defaulting to full recursion).
- 2025-12-27: Added personal-space album mirroring (`sharing.personal_album_roots`) plus AlbumService refactor so personal directories without bind mounts now receive the same automation and documentation coverage.
- 2025-12-27: Wrapped Synology Photos/web helpers in `SynologyPhotosAPI` + `SynologyWebSharing` classes with default instances so other projects can import modular helpers instead of relying on module-level globals.
- 2025-12-27: Reverted uploader permission aliases after confirming conditional albums only support `view` or `download` roles.
- 2025-12-27: Removed legacy `settings.*` compatibility exports, rewired the remaining modules to `APP_CONFIG`/`RUNTIME_STATE`, and normalized mount/media logging now that emojis were replaced with ASCII tags.
- 2025-12-27: Captured the refactor roadmap (steps 1-3) in README/TODO so subsequent cleanup work has an agreed starting point.
- 2025-12-27: Added persistent TODO tracking file and documented refactor goals per latest request.
- 2025-12-27: Split Synology web UI fallbacks into synology_albums_sync/synology_web.py and moved direct API helpers (login, folder filters, sharing, reindexing) into synology_albums_sync/synology_api.py; main.py now delegates to these modules.
- 2025-12-27: Introduced `sync_config.json`, added typed configuration loaders, and trimmed `.env` down to secrets plus an optional config pointer.
- 2025-12-27: Added Mount/Media/Album services in synology_albums_sync/services.py so `main.py` is only a CLI wrapper.
- 2025-12-27: Migrated synology_albums_sync/synology_api.py and synology_albums_sync/paths.py onto `APP_CONFIG`/`RUNTIME_STATE`, keeping API + filesystem helpers aligned with the structured config state.
- 2025-12-27: Moved `AlbumService` into synology_albums_sync/albums.py, slimmed synology_albums_sync/services.py down to `MediaService`/`MountService`, and migrated mounts/media helpers onto `APP_CONFIG`/`RUNTIME_STATE` caches.
- 2025-12-27: Migrated synology_albums_sync/synology_web.py to the typed config/runtime stack and added sequence diagrams for each CLI use case in README.md.

## Open Tasks
- (none)

