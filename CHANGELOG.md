# Synology Albums Sync - Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-01-10

### Added - Session-Based API Client

New `SynologySessionAPI` class for accessing Synology Photos using existing DSM session credentials instead of username/password authentication.

#### Why Session-Based Auth?

| Approach | Pros | Cons |
|----------|------|------|
| Username/Password | Full access, works offline | Requires credential storage |
| **Session-Based** | Uses existing login, respects user permissions | Only works in DSM context |

#### New Module: `synology_session.py`

```python
from synology_albums_sync import SynologySessionAPI, create_session_api

# Create client with DSM session
api = create_session_api(
    session_id="your_session_id",
    syno_token="csrf_token"
)

# Team Space operations
folders = api.get_all_team_folders()
root_path = api.get_team_space_root_path()
is_enabled = api.is_team_space_enabled()

# Album operations
albums = api.list_albums()
api.create_album("My Album")
api.share_album(album_id, users=["user1"], permission="view")

# User info
user = api.get_current_user()
```

#### Key Methods

| Method | Description |
|--------|-------------|
| `list_team_folders()` | List Team Space folders (respects user permissions) |
| `list_personal_folders()` | List Personal Space folders |
| `get_team_space_root_path()` | Auto-detect `/volume*/photo` path |
| `is_team_space_enabled()` | Check if Team Space is active |
| `list_albums()` | List user's albums |
| `create_album()` | Create a new album |
| `share_album()` | Share album with users |
| `list_shareable_users()` | Get users/groups for sharing |
| `get_current_user()` | Get logged-in user info |
| `get_index_status()` | Check indexing status |
| `trigger_reindex()` | Trigger photo reindex |

#### Use Cases

1. **SPK Packages**: DSM web UI embedded apps
2. **CGI Scripts**: Server-side scripts in DSM context
3. **FastAPI Integration**: Backend services with session forwarding

### Changed

- Updated `__init__.py` to export `SynologySessionAPI` and `create_session_api`
- Added `synology_session.py` to project layout documentation

## [0.1.0] - Previous Release

Initial release with:
- Bind mount management for Team Space folders
- Condition album creation and management
- Personal album roots support
- Per-user/group sharing via Photos API
- CLI with multiple operation modes
- JSON-based configuration
