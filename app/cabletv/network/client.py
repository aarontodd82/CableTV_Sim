"""Client for connecting to a CableTV server."""

import requests
from typing import Optional

from ..config import NetworkConfig


class ServerConnection:
    """Connects to a CableTV server and provides API access."""

    def __init__(self, network_config: NetworkConfig):
        self._config = network_config
        self._server_url: Optional[str] = None
        self._session = requests.Session()
        self._session.timeout = 10

    @property
    def server_url(self) -> Optional[str]:
        return self._server_url

    def connect(self) -> bool:
        """Connect to the server via manual URL or mDNS discovery.

        Returns:
            True if connection successful
        """
        # Try manual URL first
        if self._config.server_url:
            url = self._config.server_url.rstrip("/")
            if self._verify(url):
                self._server_url = url
                return True
            print(f"  Warning: Manual server_url failed: {url}")

        # Try mDNS discovery
        try:
            from .discovery import ServerDiscoverer
            discoverer = ServerDiscoverer()
            print(f"  Searching for server (timeout={self._config.discovery_timeout}s)...")
            url = discoverer.discover(timeout=self._config.discovery_timeout)
            if url and self._verify(url):
                self._server_url = url
                return True
        except ImportError:
            print("  Warning: zeroconf not installed, mDNS discovery disabled")
            print("  Install with: pip install zeroconf")
        except Exception as e:
            print(f"  Discovery error: {e}")

        return False

    def _verify(self, url: str) -> bool:
        """Verify a server URL responds correctly."""
        try:
            resp = self._session.get(f"{url}/api/server/info", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return "seed" in data
        except (requests.RequestException, ValueError):
            pass
        return False

    def get_server_info(self) -> Optional[dict]:
        """Fetch server info (seed, channels, config).

        Returns:
            Server info dict, or None on error
        """
        if not self._server_url:
            return None
        try:
            resp = self._session.get(f"{self._server_url}/api/server/info")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  Error fetching server info: {e}")
            return None

    def get_positions(self) -> dict[str, int]:
        """Fetch all series positions from server.

        Returns:
            Dict of "channel:group_key" -> position
        """
        if not self._server_url:
            return {}
        try:
            resp = self._session.get(f"{self._server_url}/api/server/positions")
            resp.raise_for_status()
            return resp.json().get("positions", {})
        except (requests.RequestException, ValueError) as e:
            print(f"  Warning: Could not fetch positions from server: {e}")
            return {}

    def advance_position(self, channel_number: int, group_key: str,
                         num_items: int, block_start_slot: int,
                         advance_by: int = 1, content_id: int = 0) -> bool:
        """Notify server of a position advance.

        Returns:
            True if server accepted the advance
        """
        if not self._server_url:
            return False
        try:
            resp = self._session.post(
                f"{self._server_url}/api/server/advance",
                json={
                    "channel_number": channel_number,
                    "group_key": group_key,
                    "num_items": num_items,
                    "block_start_slot": block_start_slot,
                    "advance_by": advance_by,
                    "content_id": content_id,
                },
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("advanced", False)
        except requests.RequestException as e:
            print(f"  Warning: Server advance failed: {e}")
        return False
