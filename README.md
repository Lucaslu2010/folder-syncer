# LAN Folder Syncer

[![License: CC BY-NC 4.0](https://img.shields.io/badge/License-CC_BY--NC_4.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nc/4.0/)

A small desktop app for sending a folder from one computer to another over the local network. Run it on both computers, choose the network adapter and folder on each, then click **Sync →** on the sender.

**The receiving folder's existing contents are deleted and replaced with the incoming contents.** Each transfer is a one-way snapshot, with no file merging or continuous background synchronization.

## Supported platforms

| Platform | Requirements |
| --- | --- |
| macOS | Python 3.10 or later with Tkinter, plus the system `ifconfig` command. |
| Windows | Python 3.10 or later with Tcl/Tk installed, and PowerShell for network-adapter discovery. |
| Linux | No dedicated supported deployment is documented. The Unix discovery path depends on `ifconfig`; Linux compatibility needs validation. |

The app uses the Python standard library only. Both computers must be able to reach each other over the selected IPv4 LAN.

## Deploy and run

Copy `folder_syncer.py` onto **both computers**. Install Python with Tkinter support, then start the app.

**macOS:**

```sh
python3 -m tkinter
python3 folder_syncer.py
```

**Windows:**

```powershell
py -m tkinter
py folder_syncer.py
```

The first command opens a small Tk test window; close it before launching the syncer. If Tkinter is missing, install a Python distribution with Tcl/Tk support. No `pip install` step is needed for normal use.

## Choose the correct network card

The adapter selector shows each interface's **name, IPv4 address, and `physical` or `VPN/tunnel` label**. Those labels are based on interface names, so check the actual connection too.

Choose the interface that connects to the **same local network as the other computer**:

| Connection | What to select |
| --- | --- |
| Both computers use Wi-Fi | Their active Wi-Fi adapters on the same LAN. |
| Both use Ethernet | Their active Ethernet adapters connected to the same LAN. |
| One uses Ethernet, one Wi-Fi | Each computer's active adapter, provided the router bridges both onto the same LAN. |
| VPN is also running | The physical Wi-Fi/Ethernet adapter carrying local traffic. |

For example, `192.168.1.20/24` and `192.168.1.35/24` are on the same subnet. Addresses on different subnets generally will not discover each other through broadcast.

On macOS, physical interfaces often have names such as `en0` or `en1`; the number alone does not tell you whether an interface is Wi-Fi or Ethernet. To identify the mapping:

```sh
networksetup -listallhardwareports
```

On Windows, use the active Wi-Fi or Ethernet entry. Avoid VPN/tunnel, disconnected, loopback, and unrelated virtual-machine adapters. Typical tunnel names include `utun`, `tun`, `tap`, WireGuard, Tailscale, and ZeroTier.

The app binds sockets to the selected interface's IP and applies interface-pinning options when available. This reduces VPN routing interference, but cannot override every system VPN or firewall policy.

## Send a folder

1. Open the app on both computers.
2. Select the correct network adapter on each.
3. Click **Choose Folder…** on each computer. The sender chooses its source; the receiver chooses the folder whose contents will be replaced.
4. Wait for the other computer and its chosen folder to appear in the peer list.
5. On the sender, select the destination peer and click **Sync →**.
6. Confirm the replacement and wait for the status result.

Files are received into a temporary staging directory before replacement begins. The sender reports success only after the receiver acknowledges that replacement succeeded. Replacement itself is not an atomic transaction; there is no automatic rollback if it fails after deleting destination files.

To send in the opposite direction, initiate a new transfer from the other computer.

## Networking and troubleshooting

- **Discovery:** UDP broadcast and direct replies on port `45454`.
- **Transfer:** TCP on a receiver port selected at runtime.
- **Firewall:** allow Python through the firewall on the private LAN on both computers. Opening only UDP discovery is insufficient for file transfer.
- **No peers:** check adapter selection, subnet, VPN policy, and router guest-network/client-isolation settings.
- **Peer appears without a target:** choose a folder on that computer.
- **Receive error:** check destination permissions and files locked by other applications.

The transfer protocol has no authentication or transport encryption. Deploy it on a trusted private LAN. Filesystem roots, drive roots, and home directories are rejected as replacement targets.

## Optional Windows executable

Build on Windows:

```powershell
py -m pip install pyinstaller
py -m PyInstaller --onefile --windowed folder_syncer.py
```

The executable is created under `dist\` and can be copied to another compatible Windows computer. Firewall access still needs to be allowed for that executable.

## License

Copyright © 2026 Lucaslu2010, for original contributions.

Except for third-party material and files with a separate license notice, the
original project source code and documentation are licensed under
[Creative Commons Attribution-NonCommercial 4.0 International
(CC BY-NC 4.0)](https://creativecommons.org/licenses/by-nc/4.0/).
See [LICENSE](LICENSE) for the full legal text.

You may share and adapt the covered material for noncommercial purposes, provided
you give appropriate credit, link to the license, and indicate changes. Commercial
uses requiring permission under copyright are not granted by this license; seek
separate permission from the relevant rights holder. The full license, including
its exceptions and limitations, governs.

Third-party code, assets, and dependencies retain their own licenses. This notice
does not replace those licenses or restrict rights they independently grant.

## Acknowledgments

Documentation and publication preparation assisted by OpenAI Codex.
