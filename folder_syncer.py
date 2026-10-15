#!/usr/bin/env python3
"""A tiny LAN folder syncer with a Tk GUI.

Run this file on two computers on the same local network. Each side chooses the
physical network adapter and a folder. Pressing Sync sends the local folder to
the paired peer, where the peer's chosen folder is replaced.
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from tkinter import BOTH, DISABLED, END, LEFT, NORMAL, StringVar, Tk, filedialog, messagebox, ttk


APP_NAME = "LAN Folder Syncer"
PROTOCOL = "lan-folder-sync-v1"
DISCOVERY_PORT = 45454
DISCOVERY_INTERVAL_SECONDS = 2.0
PEER_TIMEOUT_SECONDS = 8.0
MAX_STATUS_LINES = 100
ACK_READ_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class NetworkInterface:
    name: str
    address: str
    netmask: str
    broadcast: str
    is_likely_vpn: bool
    index: int | None = None

    @property
    def label(self) -> str:
        kind = "VPN/tunnel" if self.is_likely_vpn else "physical"
        return f"{self.name} - {self.address} ({kind})"

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(f"{self.address}/{self.netmask}", strict=False)


@dataclass
class Peer:
    device_id: str
    name: str
    ip: str
    port: int
    folder_name: str
    has_folder: bool
    last_seen: float

    @property
    def label(self) -> str:
        folder = self.folder_name if self.has_folder else "no target folder chosen"
        return f"{self.name} at {self.ip} - {folder}"


def load_device_id() -> str:
    config_dir = Path.home() / ".lan_folder_syncer"
    config_dir.mkdir(exist_ok=True)
    device_file = config_dir / "device_id"
    if device_file.exists():
        return device_file.read_text(encoding="utf-8").strip()

    device_id = str(uuid.uuid4())
    device_file.write_text(device_id, encoding="utf-8")
    return device_id


def run_ifconfig() -> str:
    return subprocess.check_output(["ifconfig"], text=True, stderr=subprocess.DEVNULL)


def run_powershell(command: str) -> str:
    candidates = [
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        ["pwsh", "-NoProfile", "-Command", command],
    ]
    for candidate in candidates:
        try:
            return subprocess.check_output(candidate, text=True, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
    return ""


def is_likely_vpn_interface(name: str) -> bool:
    lower = name.lower()
    vpn_prefixes = ("utun", "tun", "tap", "ppp", "ipsec", "wg", "zt")
    vpn_markers = (
        "vpn",
        "wireguard",
        "tailscale",
        "zerotier",
        "openvpn",
        "anyconnect",
        "globalprotect",
        "nordlynx",
        "tunnel",
    )
    return lower.startswith(vpn_prefixes) or any(marker in lower for marker in vpn_markers)


def parse_hex_netmask(hex_mask: str) -> str:
    if "." in hex_mask:
        return hex_mask
    mask_int = int(hex_mask, 16)
    return socket.inet_ntoa(struct.pack("!I", mask_int))


def enumerate_interfaces() -> list[NetworkInterface]:
    if sys.platform == "win32":
        return enumerate_windows_interfaces()
    return enumerate_ifconfig_interfaces()


def prefix_to_netmask(prefix_length: int) -> str:
    network = ipaddress.IPv4Network(f"0.0.0.0/{prefix_length}")
    return str(network.netmask)


def interface_from_address(
    name: str,
    address: str,
    netmask: str,
    is_likely_vpn: bool,
    index: int | None = None,
) -> NetworkInterface | None:
    try:
        if address.startswith("127.") or address.startswith("169.254."):
            return None
        network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
    except ValueError:
        return None

    return NetworkInterface(
        name=name,
        address=address,
        netmask=netmask,
        broadcast=str(network.broadcast_address),
        is_likely_vpn=is_likely_vpn,
        index=index,
    )


def enumerate_windows_interfaces() -> list[NetworkInterface]:
    """Enumerate active Windows IPv4 adapters through PowerShell."""
    command = (
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object { $_.AddressState -eq 'Preferred' -and "
        "$_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | "
        "Select-Object InterfaceAlias,InterfaceIndex,IPAddress,PrefixLength | "
        "ConvertTo-Json -Depth 3"
    )
    output = run_powershell(command).strip()
    if not output:
        return []

    try:
        records = json.loads(output)
    except json.JSONDecodeError:
        return []

    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        return []

    interfaces: list[NetworkInterface] = []
    for record in records:
        if not isinstance(record, dict):
            continue

        try:
            name = str(record["InterfaceAlias"])
            address = str(record["IPAddress"])
            prefix_length = int(record["PrefixLength"])
            index = int(record["InterfaceIndex"])
            netmask = prefix_to_netmask(prefix_length)
        except (KeyError, TypeError, ValueError):
            continue

        interface = interface_from_address(
            name=name,
            address=address,
            netmask=netmask,
            is_likely_vpn=is_likely_vpn_interface(name),
            index=index,
        )
        if interface is not None:
            interfaces.append(interface)

    interfaces.sort(key=lambda item: (item.is_likely_vpn, not item.name.lower().startswith(("ethernet", "wi-fi", "wifi")), item.name))
    return interfaces


def enumerate_ifconfig_interfaces() -> list[NetworkInterface]:
    """Enumerate active IPv4 adapters using macOS/BSD/Linux ifconfig output."""
    interfaces: list[NetworkInterface] = []
    current_name = ""
    current_is_active = False

    try:
        output = run_ifconfig()
    except (OSError, subprocess.SubprocessError):
        return interfaces

    for raw_line in output.splitlines():
        if raw_line and not raw_line[0].isspace():
            current_name = raw_line.split(":", 1)[0]
            current_is_active = " status: active" in raw_line or "UP," in raw_line
            continue

        line = raw_line.strip()
        if line == "status: active":
            current_is_active = True
            continue

        if not current_name or not current_is_active or not line.startswith("inet "):
            continue

        parts = line.split()
        if len(parts) < 4:
            continue

        try:
            address = parts[1]
            if "netmask" in parts:
                netmask_token = parts[parts.index("netmask") + 1]
                netmask = parse_hex_netmask(netmask_token.replace("0x", ""))
            else:
                netmask = "255.255.255.0"
        except (IndexError, ValueError, OSError):
            continue

        interface = interface_from_address(
            name=current_name,
            address=address,
            netmask=netmask,
            is_likely_vpn=is_likely_vpn_interface(current_name),
        )
        if interface is not None:
            interfaces.append(interface)

    interfaces.sort(key=lambda item: (item.is_likely_vpn, not item.name.startswith(("en", "eth")), item.name))
    return interfaces


def bind_to_interface(sock: socket.socket, interface: NetworkInterface) -> None:
    """Best-effort interface binding.

    Binding the local IP controls the source address on all platforms. On macOS
    and Windows, we also ask the OS to pin the socket to the selected adapter
    when the platform exposes a useful socket option. This helps when a
    tunnel-mode VPN has installed broad routes.
    """
    if sys.platform == "darwin":
        ip_bound_if = 25
        try:
            sock.setsockopt(socket.IPPROTO_IP, ip_bound_if, socket.if_nametoindex(interface.name))
        except OSError:
            pass
    elif sys.platform == "win32" and interface.index is not None:
        ip_unicast_if = getattr(socket, "IP_UNICAST_IF", 31)
        try:
            sock.setsockopt(socket.IPPROTO_IP, ip_unicast_if, struct.pack("I", socket.htonl(interface.index)))
        except OSError:
            pass


def address_is_on_interface(address: str, interface: NetworkInterface) -> bool:
    try:
        return ipaddress.IPv4Address(address) in interface.network
    except ValueError:
        return False


def interface_pin_modes() -> tuple[bool, ...]:
    if sys.platform in {"darwin", "win32"}:
        return (True, False)
    return (False,)


def safe_path_inside(base: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def is_dangerous_target(path: Path) -> bool:
    resolved = path.resolve()
    home = Path.home().resolve()
    blocked = {Path("/").resolve(), home, home.parent}
    if sys.platform == "win32":
        blocked.update(Path(f"{letter}:/").resolve() for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{letter}:/").exists())
    else:
        blocked.update({Path("/Users").resolve(), Path("/Volumes").resolve()})
    return resolved in blocked or resolved.parent == resolved


def replace_folder_contents(target_dir: Path, staged_dir: Path) -> None:
    if is_dangerous_target(target_dir):
        raise RuntimeError(f"Refusing to wipe unsafe target folder: {target_dir}")

    target_dir.mkdir(parents=True, exist_ok=True)
    for child in target_dir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    for child in staged_dir.iterdir():
        shutil.move(str(child), str(target_dir / child.name))


def extract_tar_stream_safely(fileobj, destination: Path) -> None:
    with tarfile.open(fileobj=fileobj, mode="r|gz") as archive:
        for member in archive:
            member_name = member.name.strip("/")
            if not member_name or member_name == ".":
                continue

            target = destination / member_name
            if Path(member_name).is_absolute() or ".." in Path(member_name).parts:
                raise RuntimeError(f"Unsafe archive path skipped: {member.name}")
            if not safe_path_inside(destination, target):
                raise RuntimeError(f"Archive path escapes target: {member.name}")

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            if not member.isfile():
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue

            with source, target.open("wb") as out:
                shutil.copyfileobj(source, out)

            try:
                os.chmod(target, member.mode)
                os.utime(target, (member.mtime, member.mtime))
            except OSError:
                pass


def send_json_line(sock: socket.socket, payload: dict[str, object]) -> None:
    sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def read_json_line_from_socket(sock: socket.socket, limit: int = 65536) -> dict[str, object]:
    data = bytearray()
    while len(data) < limit:
        chunk = sock.recv(1)
        if not chunk:
            break
        if chunk == b"\n":
            break
        data.extend(chunk)

    if not data:
        raise RuntimeError("No response from peer.")

    try:
        decoded = data.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Peer sent an invalid response: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("Peer sent an invalid response.")
    return payload


class NetworkManager:
    def __init__(self, app: "FolderSyncerApp") -> None:
        self.app = app
        self.stop_event = threading.Event()
        self.interface: NetworkInterface | None = None
        self.server_port = 0
        self.server_socket: socket.socket | None = None
        self.discovery_socket: socket.socket | None = None
        self.lock = threading.Lock()

    def restart(self, interface: NetworkInterface) -> None:
        self.stop()
        self.stop_event = threading.Event()
        self.interface = interface
        threading.Thread(target=self.run_tcp_server, name="tcp-server", daemon=True).start()
        threading.Thread(target=self.run_discovery_listener, name="discovery-listener", daemon=True).start()
        threading.Thread(target=self.run_discovery_sender, name="discovery-sender", daemon=True).start()
        self.app.post_status(f"Using {interface.name} at {interface.address}; broadcasting to {interface.broadcast}.")

    def stop(self) -> None:
        self.stop_event.set()
        for sock in (self.server_socket, self.discovery_socket):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self.server_socket = None
        self.discovery_socket = None
        with self.lock:
            self.server_port = 0

    def announce_now(self) -> None:
        interface = self.interface
        if interface is None:
            return

        with self.lock:
            server_port = self.server_port
        if server_port <= 0:
            return

        threading.Thread(target=self.send_discovery_updates, args=(interface, server_port), daemon=True).start()

    def run_tcp_server(self) -> None:
        interface = self.interface
        if interface is None:
            return

        server = None
        last_error: OSError | None = None
        for pin_interface in interface_pin_modes():
            try:
                candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if pin_interface:
                    bind_to_interface(candidate, interface)
                candidate.bind((interface.address, 0))
                candidate.listen(5)
                candidate.settimeout(1.0)
                server = candidate
                self.server_socket = server
                with self.lock:
                    self.server_port = server.getsockname()[1]
                if not pin_interface and sys.platform in {"darwin", "win32"}:
                    self.app.post_status("Receiver started with source-IP binding after OS adapter pinning failed.")
                break
            except OSError as exc:
                last_error = exc
                try:
                    candidate.close()
                except OSError:
                    pass

        if server is None:
            exc = last_error or OSError("unknown socket error")
            self.app.post_status(f"Could not start receiver on {interface.name}: {exc}")
            return

        while not self.stop_event.is_set():
            try:
                conn, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            threading.Thread(target=self.handle_incoming_sync, args=(conn, address), daemon=True).start()

    def handle_incoming_sync(self, conn: socket.socket, address: tuple[str, int]) -> None:
        target_folder = self.app.get_folder_path()
        if target_folder is None:
            self.app.post_status(f"Rejected sync from {address[0]} because no target folder is selected.")
            try:
                send_json_line(conn, {"protocol": PROTOCOL, "type": "sync_ack", "ok": False, "message": "Receiver has no target folder selected."})
            except OSError:
                pass
            conn.close()
            return

        self.app.post_status(f"Receiving folder from {address[0]}...")
        try:
            conn_file = conn.makefile("rb")
            header_line = conn_file.readline(65536)
            if not header_line:
                raise RuntimeError("Sender closed the connection before sending a header.")

            header = json.loads(header_line.decode("utf-8"))
            if header.get("protocol") != PROTOCOL or header.get("type") != "sync":
                raise RuntimeError("Unknown sync protocol.")

            with tempfile.TemporaryDirectory(prefix="folder-sync-staging-") as temp_dir:
                staged = Path(temp_dir) / "incoming"
                staged.mkdir()
                extract_tar_stream_safely(conn_file, staged)
                replace_folder_contents(target_folder, staged)

            message = f"Replaced {target_folder} with '{header.get('folder_name', 'folder')}'."
            send_json_line(conn, {"protocol": PROTOCOL, "type": "sync_ack", "ok": True, "message": message})
            self.app.post_status(message)
        except Exception as exc:
            message = f"Receive failed: {exc}"
            self.app.post_status(message)
            try:
                send_json_line(conn, {"protocol": PROTOCOL, "type": "sync_ack", "ok": False, "message": message})
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def run_discovery_listener(self) -> None:
        interface = self.interface
        if interface is None:
            return

        sock = None
        last_error: OSError | None = None
        for pin_interface in interface_pin_modes():
            try:
                candidate = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    try:
                        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    except OSError:
                        pass
                if pin_interface:
                    bind_to_interface(candidate, interface)
                candidate.bind(("", DISCOVERY_PORT))
                candidate.settimeout(1.0)
                sock = candidate
                self.discovery_socket = sock
                if not pin_interface and sys.platform in {"darwin", "win32"}:
                    self.app.post_status("Discovery listener started without OS adapter pinning.")
                break
            except OSError as exc:
                last_error = exc
                try:
                    candidate.close()
                except OSError:
                    pass

        if sock is None:
            exc = last_error or OSError("unknown socket error")
            self.app.post_status(f"Could not listen for peers on {interface.name}: {exc}")
            return

        while not self.stop_event.is_set():
            try:
                data, address = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

            if not address_is_on_interface(address[0], interface):
                continue

            if payload.get("protocol") != PROTOCOL or payload.get("device_id") == self.app.device_id:
                continue

            port = payload.get("port")
            if not isinstance(port, int) or port <= 0:
                continue

            peer = Peer(
                device_id=payload["device_id"],
                name=payload.get("name", "Unknown computer"),
                ip=address[0],
                port=port,
                folder_name=payload.get("folder_name", ""),
                has_folder=bool(payload.get("has_folder")),
                last_seen=time.time(),
            )
            self.app.update_peer(peer)

            if payload.get("type") != "reply":
                self.send_discovery_unicast(interface, port=DISCOVERY_PORT, target_ip=address[0], packet_type="reply")

    def run_discovery_sender(self) -> None:
        interface = self.interface
        if interface is None:
            return

        while not self.stop_event.is_set():
            with self.lock:
                server_port = self.server_port
            if server_port <= 0:
                time.sleep(0.2)
                continue

            self.send_discovery_broadcast(interface, server_port)

            self.stop_event.wait(DISCOVERY_INTERVAL_SECONDS)

    def send_discovery_updates(self, interface: NetworkInterface, server_port: int) -> None:
        self.send_discovery_broadcast(interface, server_port)
        for peer in self.app.peers_snapshot():
            self.send_discovery_unicast(interface, DISCOVERY_PORT, peer.ip, packet_type="reply")

    def send_discovery_broadcast(self, interface: NetworkInterface, server_port: int) -> None:
        payload = self.app.discovery_payload(server_port, packet_type="announce")
        self.send_discovery_packet(
            interface=interface,
            payload=payload,
            target=(interface.broadcast, DISCOVERY_PORT),
            failure_context="Discovery broadcast failed",
            allow_broadcast=True,
        )

    def send_discovery_unicast(self, interface: NetworkInterface, port: int, target_ip: str, packet_type: str) -> None:
        with self.lock:
            server_port = self.server_port
        if server_port <= 0:
            return

        payload = self.app.discovery_payload(server_port, packet_type=packet_type)
        self.send_discovery_packet(
            interface=interface,
            payload=payload,
            target=(target_ip, port),
            failure_context=f"Discovery reply to {target_ip} failed",
            allow_broadcast=False,
        )

    def send_discovery_packet(
        self,
        interface: NetworkInterface,
        payload: dict[str, object],
        target: tuple[str, int],
        failure_context: str,
        allow_broadcast: bool,
    ) -> None:
        last_error: OSError | None = None
        for pin_interface in interface_pin_modes():
            sock: socket.socket | None = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                if allow_broadcast:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                if pin_interface:
                    bind_to_interface(sock, interface)
                sock.bind((interface.address, 0))
                sock.sendto(json.dumps(payload).encode("utf-8"), target)
                return
            except OSError as exc:
                last_error = exc
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

        if last_error is not None:
            self.app.post_status(f"{failure_context}: {last_error}")

    def connect_to_peer(self, interface: NetworkInterface, peer: Peer) -> socket.socket:
        last_error: OSError | None = None
        for pin_interface in interface_pin_modes():
            sock: socket.socket | None = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                if pin_interface:
                    bind_to_interface(sock, interface)
                sock.bind((interface.address, 0))
                sock.settimeout(10)
                sock.connect((peer.ip, peer.port))
                sock.settimeout(None)
                return sock
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

        raise last_error or OSError("Could not connect to peer.")

    def send_sync(self, peer: Peer, source_folder: Path) -> None:
        interface = self.interface
        if interface is None:
            self.app.post_status("Choose a network adapter first.")
            return

        threading.Thread(target=self._send_sync_worker, args=(interface, peer, source_folder), daemon=True).start()

    def _send_sync_worker(self, interface: NetworkInterface, peer: Peer, source_folder: Path) -> None:
        self.app.post_status(f"Sending {source_folder} to {peer.name}...")
        try:
            sock = self.connect_to_peer(interface, peer)

            with sock:
                fileobj = sock.makefile("wb")
                header = {
                    "protocol": PROTOCOL,
                    "type": "sync",
                    "device_id": self.app.device_id,
                    "name": socket.gethostname(),
                    "folder_name": source_folder.name,
                }
                fileobj.write((json.dumps(header) + "\n").encode("utf-8"))
                fileobj.flush()

                with tarfile.open(fileobj=fileobj, mode="w|gz") as archive:
                    archive.add(source_folder, arcname=".")
                fileobj.flush()

                sock.shutdown(socket.SHUT_WR)
                sock.settimeout(ACK_READ_TIMEOUT_SECONDS)
                ack = read_json_line_from_socket(sock)

            if ack.get("protocol") != PROTOCOL or ack.get("type") != "sync_ack":
                raise RuntimeError("Peer sent an unknown sync response.")

            message = str(ack.get("message", "Sync finished."))
            if ack.get("ok") is True:
                self.app.post_status(f"Sync completed on {peer.name}: {message}")
            else:
                self.app.post_status(f"Sync rejected by {peer.name}: {message}")
        except Exception as exc:
            self.app.post_status(f"Send failed: {exc}")

class FolderSyncerApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("820x440")
        self.root.minsize(720, 390)

        self.device_id = load_device_id()
        self.folder_path: Path | None = None
        self.interfaces = enumerate_interfaces()
        self.selected_interface: NetworkInterface | None = None
        self.peers: dict[str, Peer] = {}
        self.peer: Peer | None = None
        self.ui_events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.network = NetworkManager(self)

        self.interface_var = StringVar()
        self.local_folder_var = StringVar(value="No folder chosen")
        self.peer_var = StringVar(value="Waiting for another computer...")
        self.status_var = StringVar(value="Choose a folder on both computers, then press Sync on the sender.")

        self.build_ui()
        self.refresh_interfaces()
        self.root.after(200, self.process_ui_events)
        self.root.after(1000, self.expire_peers)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=BOTH, expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(0, 14))

        ttk.Label(top, text="Network card").pack(side=LEFT)
        self.interface_combo = ttk.Combobox(top, textvariable=self.interface_var, state="readonly", width=54)
        self.interface_combo.pack(side=LEFT, padx=(10, 8))
        self.interface_combo.bind("<<ComboboxSelected>>", self.on_interface_selected)

        ttk.Button(top, text="Refresh", command=self.refresh_interfaces).pack(side=LEFT)

        main = ttk.Frame(outer)
        main.pack(fill=BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=0)
        main.columnconfigure(2, weight=1)
        main.rowconfigure(0, weight=1)

        left_box = ttk.LabelFrame(main, text="This computer", padding=14)
        left_box.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        left_box.rowconfigure(1, weight=1)
        left_box.columnconfigure(0, weight=1)

        ttk.Label(left_box, text=socket.gethostname(), font=("TkDefaultFont", 14, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(left_box, textvariable=self.local_folder_var, wraplength=260).grid(row=1, column=0, sticky="nw", pady=(14, 0))

        middle = ttk.Frame(main)
        middle.grid(row=0, column=1, sticky="ns")
        middle.rowconfigure(0, weight=1)
        middle.rowconfigure(4, weight=1)

        self.sync_button = ttk.Button(middle, text="Sync ->", command=self.on_sync, state=DISABLED)
        self.sync_button.grid(row=1, column=0, pady=(70, 10), ipadx=12, ipady=8)

        ttk.Button(middle, text="Choose Folder...", command=self.choose_folder).grid(row=2, column=0, pady=(0, 8))
        ttk.Label(middle, text="Receiver's folder is wiped\nbefore replacement.", justify="center").grid(row=3, column=0)

        right_box = ttk.LabelFrame(main, text="Paired computer", padding=14)
        right_box.grid(row=0, column=2, sticky="nsew", padx=(16, 0))
        right_box.rowconfigure(1, weight=1)
        right_box.columnconfigure(0, weight=1)

        ttk.Label(right_box, text="LAN peer", font=("TkDefaultFont", 14, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(right_box, textvariable=self.peer_var, wraplength=260).grid(row=1, column=0, sticky="nw", pady=(14, 0))

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(14, 0))
        ttk.Label(bottom, textvariable=self.status_var).pack(side=LEFT, fill="x", expand=True)

        self.log = ttk.Treeview(outer, columns=("message",), show="headings", height=5)
        self.log.heading("message", text="Activity")
        self.log.column("message", stretch=True)
        self.log.pack(fill="x", pady=(10, 0))

    def refresh_interfaces(self) -> None:
        self.interfaces = enumerate_interfaces()
        labels = [item.label for item in self.interfaces]
        self.interface_combo["values"] = labels

        if not self.interfaces:
            self.post_status("No active IPv4 network adapters found.")
            return

        chosen_index = 0
        if self.selected_interface is not None:
            for index, interface in enumerate(self.interfaces):
                if interface.name == self.selected_interface.name and interface.address == self.selected_interface.address:
                    chosen_index = index
                    break

        self.interface_combo.current(chosen_index)
        self.selected_interface = self.interfaces[chosen_index]
        self.network.restart(self.selected_interface)

    def on_interface_selected(self, _event=None) -> None:
        index = self.interface_combo.current()
        if index < 0 or index >= len(self.interfaces):
            return

        self.selected_interface = self.interfaces[index]
        self.peers.clear()
        self.peer = None
        self.peer_var.set("Waiting for another computer...")
        self.update_sync_state()
        self.network.restart(self.selected_interface)

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="Choose folder to sync")
        if not selected:
            return

        folder = Path(selected)
        if is_dangerous_target(folder):
            messagebox.showerror(APP_NAME, "Please choose a normal project or transfer folder, not your home folder or a system root.")
            return

        self.folder_path = folder
        self.local_folder_var.set(str(folder))
        self.post_status(f"Selected folder: {folder}")
        self.update_sync_state()
        self.network.announce_now()

    def on_sync(self) -> None:
        if self.folder_path is None or self.peer is None:
            return

        if not self.peer.has_folder:
            messagebox.showwarning(APP_NAME, "The paired computer has not chosen a target folder yet.")
            return

        confirmed = messagebox.askyesno(
            APP_NAME,
            f"Send '{self.folder_path.name}' to {self.peer.name}?\n\n"
            "The paired computer will wipe its chosen folder before replacing it.",
        )
        if not confirmed:
            return

        self.network.send_sync(self.peer, self.folder_path)

    def get_folder_path(self) -> Path | None:
        return self.folder_path

    def discovery_payload(self, port: int, packet_type: str) -> dict[str, object]:
        folder_name = self.folder_path.name if self.folder_path is not None else ""
        return {
            "protocol": PROTOCOL,
            "type": packet_type,
            "device_id": self.device_id,
            "name": socket.gethostname(),
            "port": port,
            "has_folder": self.folder_path is not None,
            "folder_name": folder_name,
        }

    def peers_snapshot(self) -> list[Peer]:
        return list(self.peers.values())

    def update_peer(self, peer: Peer) -> None:
        self.ui_events.put(("peer", peer))

    def post_status(self, message: str) -> None:
        self.ui_events.put(("status", message))

    def process_ui_events(self) -> None:
        while True:
            try:
                kind, payload = self.ui_events.get_nowait()
            except queue.Empty:
                break

            if kind == "peer":
                self.apply_peer(payload)
            elif kind == "status":
                self.apply_status(str(payload))

        self.root.after(200, self.process_ui_events)

    def apply_peer(self, peer: Peer) -> None:
        was_empty = not self.peers
        self.peers[peer.device_id] = peer
        self.peer = sorted(self.peers.values(), key=lambda item: item.last_seen, reverse=True)[0]
        self.peer_var.set(self.peer.label)
        self.update_sync_state()
        if was_empty:
            self.apply_status(f"Paired with {peer.name} at {peer.ip}.")

    def apply_status(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.status_var.set(message)
        self.log.insert("", END, values=(f"{timestamp}  {message}",))
        children = self.log.get_children()
        if len(children) > MAX_STATUS_LINES:
            self.log.delete(*children[: len(children) - MAX_STATUS_LINES])
        self.log.yview_moveto(1)

    def expire_peers(self) -> None:
        now = time.time()
        expired = [device_id for device_id, peer in self.peers.items() if now - peer.last_seen > PEER_TIMEOUT_SECONDS]
        for device_id in expired:
            self.peers.pop(device_id, None)

        if self.peer is not None and self.peer.device_id not in self.peers:
            self.peer = None
            self.peer_var.set("Waiting for another computer...")
            self.apply_status("Peer disconnected.")
        elif self.peers:
            self.peer = sorted(self.peers.values(), key=lambda item: item.last_seen, reverse=True)[0]
            self.peer_var.set(self.peer.label)

        self.update_sync_state()
        self.root.after(1000, self.expire_peers)

    def update_sync_state(self) -> None:
        can_sync = self.folder_path is not None and self.peer is not None and self.peer.has_folder
        self.sync_button.configure(state=NORMAL if can_sync else DISABLED)

    def on_close(self) -> None:
        self.network.stop()
        self.root.destroy()


def main() -> None:
    root = Tk()
    style = ttk.Style(root)
    if "aqua" in style.theme_names():
        style.theme_use("aqua")
    FolderSyncerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
