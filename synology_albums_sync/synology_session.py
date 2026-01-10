"""Session-based Synology API client for use with existing DSM sessions.

This module provides API access using an existing DSM session (cookie + SynoToken)
instead of username/password authentication. Useful for:
- SPK packages running in DSM context
- Web UI components embedded in DSM
- Any scenario where user is already logged into DSM

Usage:
    from synology_albums_sync.synology_session import SynologySessionAPI
    
    api = SynologySessionAPI(
        host="localhost",
        port=5000,
        session_id="Tm3C43BGm1e9Grs4pLZS...",
        syno_token="xxx..."
    )
    
    # List Team Space folders
    folders = api.list_team_folders()
    
    # Get personal folder filters
    filters = api.list_folder_filters()
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    URLLIB_AVAILABLE = True
except ImportError:
    URLLIB_AVAILABLE = False


class SynologySessionAPI:
    """Synology API client using existing DSM session credentials."""
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 5000,
        session_id: str = "",
        syno_token: str = "",
        secure: bool = False,
        timeout: int = 30,
    ):
        """Initialize the session-based API client.
        
        Args:
            host: DSM hostname or IP (default: localhost for local calls)
            port: DSM port (default: 5000 for HTTP, use 5001 for HTTPS)
            session_id: The 'id' cookie value from DSM session
            syno_token: The X-SYNO-TOKEN header value for CSRF protection
            secure: Use HTTPS if True
            timeout: Request timeout in seconds
        """
        self.host = host or "localhost"
        self.port = port
        self.session_id = session_id
        self.syno_token = syno_token
        self.secure = secure
        self.timeout = timeout
        
        scheme = "https" if secure else "http"
        self.base_url = f"{scheme}://{self.host}:{self.port}"
        self.api_endpoint = f"{self.base_url}/webapi/entry.cgi"
    
    def _make_request(
        self,
        api: str,
        method: str,
        version: int = 1,
        params: Optional[Dict[str, Any]] = None,
        http_method: str = "POST",
    ) -> Dict[str, Any]:
        """Make an API request using session credentials.
        
        Args:
            api: API name (e.g., "SYNO.FotoTeam.Browse.Folder")
            method: API method (e.g., "list")
            version: API version
            params: Additional parameters
            http_method: HTTP method (GET or POST)
            
        Returns:
            API response as dictionary
            
        Raises:
            RuntimeError: If request fails
        """
        request_params = {
            "api": api,
            "method": method,
            "version": version,
            **(params or {}),
        }
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        
        if self.syno_token:
            headers["X-SYNO-TOKEN"] = self.syno_token
        
        cookies = {}
        if self.session_id:
            cookies["id"] = self.session_id
        
        # Try requests library first (more reliable)
        if requests is not None:
            return self._request_with_requests(
                request_params, headers, cookies, http_method
            )
        
        # Fallback to urllib
        if URLLIB_AVAILABLE:
            return self._request_with_urllib(
                request_params, headers, cookies, http_method
            )
        
        raise RuntimeError("No HTTP library available (install 'requests' package)")
    
    def _request_with_requests(
        self,
        params: Dict[str, Any],
        headers: Dict[str, str],
        cookies: Dict[str, str],
        http_method: str,
    ) -> Dict[str, Any]:
        """Make request using the requests library."""
        try:
            if http_method.upper() == "GET":
                response = requests.get(
                    self.api_endpoint,
                    params=params,
                    headers=headers,
                    cookies=cookies,
                    timeout=self.timeout,
                    verify=False,  # DSM often uses self-signed certs
                )
            else:
                response = requests.post(
                    self.api_endpoint,
                    data=params,
                    headers=headers,
                    cookies=cookies,
                    timeout=self.timeout,
                    verify=False,
                )
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Request failed: {e}") from e
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON response: {e}") from e
    
    def _request_with_urllib(
        self,
        params: Dict[str, Any],
        headers: Dict[str, str],
        cookies: Dict[str, str],
        http_method: str,
    ) -> Dict[str, Any]:
        """Make request using urllib (fallback)."""
        try:
            encoded_params = urlencode(params)
            
            if http_method.upper() == "GET":
                url = f"{self.api_endpoint}?{encoded_params}"
                data = None
            else:
                url = self.api_endpoint
                data = encoded_params.encode("utf-8")
            
            req = Request(url, data=data, headers=headers, method=http_method)
            
            # Add cookie header
            if cookies:
                cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                req.add_header("Cookie", cookie_str)
            
            with urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
                
        except (HTTPError, URLError) as e:
            raise RuntimeError(f"Request failed: {e}") from e
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON response: {e}") from e
    
    # =========================================================================
    # Team Space APIs
    # =========================================================================
    
    def list_team_folders(
        self,
        folder_id: int = 0,
        offset: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """List folders in Team Space.
        
        Args:
            folder_id: Parent folder ID (0 for root)
            offset: Pagination offset
            limit: Maximum number of results
            
        Returns:
            API response with folder list
        """
        return self._make_request(
            api="SYNO.FotoTeam.Browse.Folder",
            method="list",
            version=1,
            params={
                "id": folder_id,
                "offset": offset,
                "limit": limit,
            },
        )
    
    def get_team_folder(self, folder_id: int) -> Dict[str, Any]:
        """Get details of a Team Space folder.
        
        Args:
            folder_id: Folder ID
            
        Returns:
            API response with folder details
        """
        return self._make_request(
            api="SYNO.FotoTeam.Browse.Folder",
            method="get",
            version=1,
            params={"id": folder_id},
        )
    
    # =========================================================================
    # Personal Space APIs
    # =========================================================================
    
    def list_personal_folders(
        self,
        folder_id: int = 0,
        offset: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """List folders in Personal Space.
        
        Args:
            folder_id: Parent folder ID (0 for root)
            offset: Pagination offset
            limit: Maximum number of results
            
        Returns:
            API response with folder list
        """
        return self._make_request(
            api="SYNO.Foto.Browse.Folder",
            method="list",
            version=1,
            params={
                "id": folder_id,
                "offset": offset,
                "limit": limit,
            },
        )
    
    def list_folder_filters(self) -> Dict[str, Any]:
        """List folder filters (for personal space folder discovery).
        
        Returns:
            API response with folder filter list
        """
        return self._make_request(
            api="SYNO.Foto.Search.Filter",
            method="list",
            version=1,
        )
    
    # =========================================================================
    # Album APIs
    # =========================================================================
    
    def list_albums(
        self,
        offset: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """List albums.
        
        Args:
            offset: Pagination offset
            limit: Maximum number of results
            
        Returns:
            API response with album list
        """
        return self._make_request(
            api="SYNO.Foto.Browse.Album",
            method="list",
            version=1,
            params={
                "offset": offset,
                "limit": limit,
            },
        )
    
    def create_album(self, name: str) -> Dict[str, Any]:
        """Create a new album.
        
        Args:
            name: Album name
            
        Returns:
            API response with created album details
        """
        return self._make_request(
            api="SYNO.Foto.Browse.Album",
            method="create",
            version=1,
            params={"name": name},
        )
    
    # =========================================================================
    # Sharing APIs
    # =========================================================================
    
    def list_shareable_users(self) -> Dict[str, Any]:
        """List users and groups that can be shared with.
        
        Returns:
            API response with shareable targets
        """
        return self._make_request(
            api="SYNO.Foto.Sharing.Misc",
            method="list_user_group",
            version=1,
            params={"team_space_sharable_list": False},
        )
    
    def share_album(
        self,
        album_id: int,
        users: List[str],
        permission: str = "view",
    ) -> Dict[str, Any]:
        """Share an album with users.
        
        Args:
            album_id: Album ID
            users: List of usernames to share with
            permission: Permission level (view, download, manage)
            
        Returns:
            API response
        """
        return self._make_request(
            api="SYNO.Foto.Sharing.Album",
            method="set_shared",
            version=1,
            params={
                "album_id": album_id,
                "enabled": True,
                "privacy_type": "private",
                "users": json.dumps(users),
                "permission": permission,
            },
        )
    
    # =========================================================================
    # Index APIs
    # =========================================================================
    
    def get_index_status(self) -> Dict[str, Any]:
        """Get the current indexing status.
        
        Returns:
            API response with index status
        """
        return self._make_request(
            api="SYNO.Foto.Index",
            method="get",
            version=1,
        )
    
    def trigger_reindex(self) -> Dict[str, Any]:
        """Trigger a reindex of personal photos.
        
        Returns:
            API response
        """
        return self._make_request(
            api="SYNO.Foto.Index",
            method="reindex",
            version=1,
            params={"type": "basic"},
        )
    
    # =========================================================================
    # DSM APIs (for package/system info)
    # =========================================================================
    
    def get_shared_folder(self, name: str = "photo") -> Dict[str, Any]:
        """Get information about a shared folder.
        
        Args:
            name: Shared folder name
            
        Returns:
            API response with shared folder details
        """
        return self._make_request(
            api="SYNO.Core.Share",
            method="get",
            version=1,
            params={"name": name},
        )
    
    def list_shared_folders(self) -> Dict[str, Any]:
        """List all shared folders.
        
        Returns:
            API response with shared folder list
        """
        return self._make_request(
            api="SYNO.Core.Share",
            method="list",
            version=1,
            params={
                "offset": 0,
                "limit": 100,
                "sort_by": "name",
                "sort_direction": "asc",
            },
        )
    
    def get_package_status(self, package_name: str) -> Dict[str, Any]:
        """Get status of an installed package.
        
        Args:
            package_name: Package name
            
        Returns:
            API response with package status
        """
        return self._make_request(
            api="SYNO.Core.Package",
            method="get",
            version=1,
            params={"id": package_name},
        )
    
    # =========================================================================
    # User Info APIs
    # =========================================================================
    
    def get_current_user(self) -> Dict[str, Any]:
        """Get information about the current logged-in user.
        
        Returns:
            API response with user details
        """
        return self._make_request(
            api="SYNO.Foto.UserInfo",
            method="get",
            version=1,
        )
    
    # =========================================================================
    # Convenience Methods
    # =========================================================================
    
    def is_team_space_enabled(self) -> bool:
        """Check if Team Space is enabled in Synology Photos.
        
        Returns:
            True if Team Space is enabled and accessible
        """
        try:
            result = self.list_team_folders(folder_id=0, limit=1)
            return result.get("success", False)
        except Exception:
            return False
    
    def get_team_space_root_path(self) -> Optional[str]:
        """Get the filesystem path to Team Space root.
        
        Returns:
            Path string or None if not found
        """
        try:
            result = self.get_shared_folder("photo")
            if result.get("success"):
                data = result.get("data", {})
                # Try different possible keys for the path
                for key in ("vol_path", "path", "additional", "mount_point_of_partition"):
                    if key in data and data[key]:
                        return str(data[key])
                # Check inside 'shares' if present
                shares = data.get("shares", [])
                if shares and isinstance(shares, list):
                    return shares[0].get("path")
        except Exception:
            pass
        return None
    
    def get_all_team_folders(self) -> List[Dict[str, Any]]:
        """Get all folders in Team Space root.
        
        Returns:
            List of folder dictionaries with 'name' and 'id' keys
        """
        try:
            result = self.list_team_folders(folder_id=0, limit=1000)
            if result.get("success"):
                return result.get("data", {}).get("list", [])
        except Exception:
            pass
        return []


# Convenience factory function
def create_session_api(
    session_id: str,
    syno_token: str,
    host: str = "localhost",
    port: int = 5000,
) -> SynologySessionAPI:
    """Create a session-based API client.
    
    Args:
        session_id: DSM session ID (from 'id' cookie)
        syno_token: CSRF token (from X-SYNO-TOKEN header or URL param)
        host: DSM host
        port: DSM port
        
    Returns:
        Configured SynologySessionAPI instance
    """
    return SynologySessionAPI(
        host=host,
        port=port,
        session_id=session_id,
        syno_token=syno_token,
    )


__all__ = [
    "SynologySessionAPI",
    "create_session_api",
]
