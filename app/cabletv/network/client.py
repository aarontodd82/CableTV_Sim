"""Client for connecting to a CableTV server."""

import time
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

    def measure_clock_offset(self, samples: int = 5) -> float:
        """Measure the clock offset between this machine and the server.

        Uses multiple samples and takes the median to reduce noise.
        Positive offset means server clock is ahead of local clock.

        Returns:
            Offset in seconds (add to local time to match server)
        """
        if not self._server_url:
            return 0.0

        offsets = []
        for _ in range(samples):
            try:
                t1 = time.time()
                resp = self._session.get(
                    f"{self._server_url}/api/server/time", timeout=5)
                t2 = time.time()
                if resp.status_code == 200:
                    server_time = resp.json()["time"]
                    rtt = t2 - t1
                    # Server timestamp was captured mid-round-trip
                    estimated_server_now = server_time + rtt / 2
                    offset = estimated_server_now - t2
                    offsets.append(offset)
            except (requests.RequestException, ValueError, KeyError):
                pass

        if not offsets:
            return 0.0

        # Median is more robust than mean against outliers
        offsets.sort()
        return offsets[len(offsets) // 2]

