# Synology Albums Sync API Usage

This guide shows how to embed Synology Albums Sync's helper classes (`SynologyPhotosAPI` and `SynologyWebSharing`) in other projects without pulling in the CLI.

## Prerequisites

1. Provide the usual `.env` secrets (`SYNOLOGY_*`) plus a `sync_config.json` file. You can reuse the ones that ship with Synology Albums Sync or supply your own.
2. Install the same dependencies listed in `requirements.txt` (at minimum `synology-api`, `python-dotenv`, and `pyotp`).
3. Decide whether you want to reuse the default config/runtime (`config.APP_CONFIG` / `config.RUNTIME_STATE`) or instantiate fresh copies per process with `load_app_config()` + `build_runtime_state()`.

## Quick-start example

```python
from synology_albums_sync import (
    SynologyPhotosAPI,
    SynologyWebSharing,
    build_runtime_state,
    load_app_config,
)
from synology_albums_sync import config

# Option A: reuse the already-loaded config/runtime
app_config = config.APP_CONFIG
runtime_state = config.RUNTIME_STATE

# Option B: load a standalone copy (useful inside other services)
# app_config = load_app_config()
# runtime_state = build_runtime_state(app_config)

api = SynologyPhotosAPI(
    app_config=app_config,
    runtime_state=runtime_state,
    web_sharing=SynologyWebSharing(
        host=app_config.security.ip,
        port=app_config.security.port,
        share_link_base=app_config.sharing.share_link_url_base,
        runtime_state=runtime_state,
    ),
)

api.ensure_photos_session()           # Login + cache folder filters
albums = api.fetch_existing_albums()  # Native synology-api call wrapper

paths = ["/volume1/homes/photos_sync/Photos/Family"]
missing = api.wait_for_paths_indexed(paths, label="family root warm-up")
if missing:
    api.log_unindexed_paths("family root warm-up", missing)
```

The same `SynologyPhotosAPI` instance works anywhere you can import the package, so background jobs, REST endpoints, or notebooks can all share the logic.

## Sharing albums programmatically

```python
api.ensure_photos_session()
api.share_album(
    album_id=42,
    album_name="Family - Trips",
    share_with=["family_rw"],
    permission="download",
    share_roles=["downloader"],
)
```

If the installed `synology-api` build does not support per-user sharing, the helper automatically falls back to `SynologyWebSharing.apply_private_sharing()` (which mimics the DSM web UI flow). You can invoke the web helper directly when you need to wire sharing logic into other workflows:

```python
sharing = SynologyWebSharing(
    host="nas.local",
    port=5001,
    share_link_base="https://photos.example.com",
    runtime_state=runtime_state,
)
sharing.apply_private_sharing(
    target_label="Kids Archive",
    share_with=["kids_group"],
    permission="view",
    share_roles=["viewer"],
    api_name="SYNO.Foto.Sharing.Passphrase",
    policy="album",
    policy_kwargs={"album_id": 1234},
)
```

`SynologyWebSharing` also exposes helpers such as `format_public_share_url()` and `build_permission_entries()` so you can control how passphrases and invite lists are rendered.

## Plug-and-play tips

- Instantiate a fresh `RuntimeState` per worker if you plan to multi-thread or multi-process; the cache fields are not thread-safe.
- Call `ensure_photos_session()` once per process, then reuse the API object—it caches folder filters, share capabilities, and targeted reindex flags for you.
- Use `request_targeted_reindex()` or `trigger_personal_reindex()` when your project stages files outside the main CLI but still needs DSM indexing nudges.
- The helpers raise standard Python exceptions; wrap high-level calls in your framework's retry/backoff strategy if you need resiliency.
- Keep `SynologyWebSharing` around if you rely on manual invite flows—the helper caches user/group IDs so repeated calls stay fast.

With these pieces, Synology Albums Sync doubles as a reusable library: import the modules you need, wire them into your scheduler or service container, and keep leveraging the same DSM-specific logic without rewriting it.
