"""First-run SMB/network share setup instructions for server mode."""

import os
import socket
import sys
from pathlib import Path

from ..platform import get_drive_root


def should_show_instructions() -> bool:
    """Check if SMB setup instructions should be shown."""
    marker = get_drive_root() / ".server_setup_done"
    return not marker.exists()


def print_smb_instructions() -> None:
    """Print platform-specific network share setup instructions."""
    root = get_drive_root()
    hostname = socket.gethostname()

    # Try to get LAN IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        ip = "<your-server-ip>"

    print()
    print("=" * 60)
    print("  NETWORK SHARE SETUP")
    print("=" * 60)
    print()
    print("  Remote clients need access to the content files.")
    print(f"  Share the CableTV root folder as a network share:")
    print()
    print(f"  Folder: {root}")
    print(f"  Host:   {hostname} ({ip})")
    print()

    if sys.platform == "win32":
        print("  WINDOWS SETUP:")
        print(f"  1. Right-click '{root}' > Properties > Sharing > Share")
        print("  2. Add 'Everyone' with Read permissions")
        print("  3. Click Share, then Done")
        print()
        print("  Remote clients set content_root in config.yaml to:")
        print(f"    \\\\{hostname}\\{root.name}")
        print(f"    or: \\\\{ip}\\{root.name}")
    else:
        print("  LINUX SETUP (Samba):")
        print("  Add to /etc/samba/smb.conf:")
        print()
        print(f"  [CableTV_Sim]")
        print(f"    path = {root}")
        print(f"    browseable = yes")
        print(f"    read only = yes")
        print(f"    guest ok = yes")
        print()
        print("  Then: sudo systemctl restart smbd")
        print()
        print("  Remote clients set content_root in config.yaml to:")
        print(f"    //{hostname}/CableTV_Sim")
        print(f"    or: //{ip}/CableTV_Sim")

    print()
    print("=" * 60)
    print()

    # Create marker file
    marker = root / ".server_setup_done"
    try:
        marker.touch()
    except OSError:
        pass
