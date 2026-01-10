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

## Session-based API (for DSM Web UI)

When building applications that run inside DSM's web interface (SPK packages, CGI scripts, iframes), you can use `SynologySessionAPI` instead of username/password authentication. This approach uses the user's existing DSM login session.

### When to use Session-based API

| Scenario | Recommended API |
|----------|-----------------|
| Background service / CLI tool | `SynologyPhotosAPI` (username/password) |
| DSM web UI / SPK package | `SynologySessionAPI` (session credentials) |
| CGI script in DSM context | `SynologySessionAPI` |
| Mobile app connecting to NAS | `SynologyPhotosAPI` |

### Quick-start example

```python
from synology_albums_sync import SynologySessionAPI, create_session_api

# Create client with DSM session credentials
api = create_session_api(
    session_id="Tm3C43BGm1e9Grs4pLZS...",  # From 'id' cookie
    syno_token="xxx...",             # From X-SYNO-TOKEN header
    host="127.0.0.1",                       # localhost for CGI/SPK
    port=5000                               # DSM HTTP port
)

# Check Team Space availability
if api.is_team_space_enabled():
    print("Team Space is enabled!")
    
    # Get photo root path
    root = api.get_team_space_root_path()
    print(f"Team Space path: {root}")
    
    # List folders (respects current user's permissions)
    folders = api.get_all_team_folders()
    for f in folders:
        print(f"  - {f['name']}")
```

### Getting session credentials in JavaScript (DSM iframe)

```javascript
// Inside an iframe embedded in DSM:
function getDsmCredentials() {
    try {
        const sid = window.parent.SYNO.SDS.Session.SID;
        const token = window.parent.SYNO.SDS.Session.SynoToken;
        return { sessionId: sid, synoToken: token };
    } catch (e) {
        console.error('Not running in DSM context');
        return null;
    }
}
```

### CGI script example (shell + curl)

```bash
#!/bin/sh
# Extract session from cookie
SESSION_ID=$(echo "$HTTP_COOKIE" | grep -oP 'id=\K[^;]+')
SYNO_TOKEN="$1"  # Passed from query string

# Call Photos API with session auth
curl -s --max-time 10 \
    -X POST \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -H "X-SYNO-TOKEN: $SYNO_TOKEN" \
    -b "id=$SESSION_ID" \
    -d "api=SYNO.FotoTeam.Browse.Folder&version=1&method=list&id=0&limit=100" \
    "http://127.0.0.1:5000/webapi/entry.cgi"
```

### Available methods

| Method | Description |
|--------|-------------|
| `list_team_folders(folder_id, offset, limit)` | List Team Space folders |
| `get_team_folder(folder_id)` | Get folder details |
| `list_personal_folders(folder_id, offset, limit)` | List Personal Space folders |
| `list_folder_filters()` | Get folder filter entries |
| `list_albums(offset, limit)` | List albums |
| `create_album(name)` | Create album |
| `share_album(album_id, users, permission)` | Share album |
| `list_shareable_users()` | Get users/groups for sharing |
| `get_index_status()` | Check indexing status |
| `trigger_reindex()` | Trigger photo reindex |
| `get_shared_folder(name)` | Get shared folder info |
| `list_shared_folders()` | List all shared folders |
| `get_package_status(package_name)` | Get package status |
| `get_current_user()` | Get current user info |
| `is_team_space_enabled()` | Check if Team Space active |
| `get_team_space_root_path()` | Auto-detect photo root |
| `get_all_team_folders()` | Get all Team Space folders |

### Key differences from SynologyPhotosAPI

```
SynologyPhotosAPI (credential-based):
  ┌─────────────────────────────────────────────────┐
  │  Your App  →  synology-api library  →  DSM API  │
  │                    ↑                            │
  │           username + password                   │
  └─────────────────────────────────────────────────┘
  
SynologySessionAPI (session-based):
  ┌─────────────────────────────────────────────────┐
  │  Your App  →  direct HTTP calls  →  DSM API    │
  │                    ↑                            │
  │           session cookie + CSRF token           │
  │           (from logged-in user)                 │
  └─────────────────────────────────────────────────┘
```

The session-based approach inherits the current user's permissions, so a non-admin user will only see the Team Space folders they have access to.