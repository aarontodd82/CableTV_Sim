"""mDNS service advertisement and discovery for CableTV server/remote."""

import socket
import threading
from typing import Optional

from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

SERVICE_TYPE = "_cabletv._tcp.local."


class ServerAdvertiser:
    """Advertises a CableTV server via mDNS/zeroconf."""

    def __init__(self, port: int, server_name: str = "CableTV Server"):
        self._port = port
        self._server_name = server_name
        self._zeroconf: Optional[Zeroconf] = None
        self._info: Optional[ServiceInfo] = None

    def start(self) -> None:
        """Register the mDNS service."""
        local_ip = self._get_local_ip()
        hostname = socket.gethostname()

        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{self._server_name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(local_ip)],
            port=self._port,
            properties={
                "hostname": hostname,
                "version": "1",
            },
        )

        self._zeroconf = Zeroconf()
        self._zeroconf.register_service(self._info)
        print(f"  mDNS: Advertising as '{self._server_name}' at {local_ip}:{self._port}")

    def stop(self) -> None:
        """Unregister and close."""
        if self._zeroconf and self._info:
            self._zeroconf.unregister_service(self._info)
            self._zeroconf.close()
            self._zeroconf = None
            self._info = None

    @staticmethod
    def _get_local_ip() -> str:
        """Get the LAN IP address using UDP socket trick."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "127.0.0.1"


class _DiscoveryListener(ServiceListener):
    """Internal listener that captures the first discovered service."""

    def __init__(self):
        self.result: Optional[str] = None
        self._event = threading.Event()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info and info.addresses:
            ip = socket.inet_ntoa(info.addresses[0])
            port = info.port
            self.result = f"http://{ip}:{port}"
            self._event.set()

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def wait(self, timeout: float) -> Optional[str]:
        self._event.wait(timeout=timeout)
        return self.result


class ServerDiscoverer:
    """Discovers a CableTV server on the LAN via mDNS."""

    def discover(self, timeout: int = 10) -> Optional[str]:
        """Search for a CableTV server.

        Args:
            timeout: Seconds to wait for discovery

        Returns:
            Server URL (e.g. "http://192.168.1.100:5000") or None
        """
        zc = Zeroconf()
        listener = _DiscoveryListener()
        try:
            ServiceBrowser(zc, SERVICE_TYPE, listener)
            return listener.wait(timeout)
        finally:
            zc.close()
