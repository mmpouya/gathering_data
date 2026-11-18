"""Run one isolated NekoRay instance per exit and write a crawler proxy list.

NekoRay keeps one active profile per process. To rotate exit IPs this helper
clones its config into separate appdata directories, selects a profile per
instance, gives each instance its own local HTTP port, and starts it with
``nekoray.exe -many -appdata ... -tray``.

Windows only: ``nekoray.exe`` requires elevation, so run this from an elevated
terminal or pass ``--elevate`` (one UAC prompt for the whole batch).
"""

from __future__ import annotations

import argparse
import ctypes
import http.client
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_NEKORAY_DIR = Path(os.environ.get("NEKORAY_DIR", r"D:\Program\nekoray"))
DEFAULT_PROFILES = [0, 3, 6, 22]
DEFAULT_BASE_PORT = 12100
PROBE_URL = "https://api.ipify.org"


def _default_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "crawl_hadith" / "nekoray-pool"


def _configure_console() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


def _safe(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, "replace").decode(encoding, "replace")


def load_profiles(config_dir: Path) -> dict[int, dict[str, Any]]:
    profiles: dict[int, dict[str, Any]] = {}
    for path in sorted((config_dir / "profiles").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            profiles[int(path.stem)] = data
        except (OSError, ValueError):
            continue
    return profiles


def profile_label(profile_id: int, data: dict[str, Any]) -> str:
    bean = data.get("bean") or {}
    name = str(bean.get("name") or "")
    kind = str(data.get("type") or "")
    return f"{profile_id}: {kind} {name}".strip()


def prepare_instance(
    source: Path,
    dest: Path,
    profile_id: int,
    socks_port: int,
    http_port: int,
) -> dict[str, Any]:
    """Clone the NekoRay config into ``dest`` and select one profile."""
    if not (source / "profiles" / f"{profile_id}.json").exists():
        raise ValueError(f"profile {profile_id} not found in {source / 'profiles'}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in ("profiles", "groups", "routes"):
        if (source / name).is_dir():
            shutil.copytree(source / name, dest / name)

    settings_path = dest / "groups" / "nekoray.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings.update(
        {
            "inbound_address": "127.0.0.1",
            "inbound_socks_port": socks_port,
            "inbound_http_port": http_port,
            "remember_enable": True,
            "remember_id": profile_id,
            "spmode2": [],
            "start_minimal": True,
        }
    )
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=4), encoding="utf-8"
    )
    return settings


def is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate() -> int:
    """Relaunch this command with one UAC prompt; all children inherit it."""
    if sys.platform != "win32":
        print("NekoRay pool is Windows-only", file=sys.stderr)
        return 1
    script = Path(__file__).resolve()
    args = [arg for arg in sys.argv[1:] if arg != "--elevate"]
    params = subprocess.list2cmdline([str(script), *args])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, str(Path.cwd()), 1
        )
    except Exception as exc:
        print(f"could not elevate: {exc}", file=sys.stderr)
        return 1
    if rc <= 32:
        print(f"elevation failed (ShellExecuteW={rc})", file=sys.stderr)
        return 1
    return 0


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_port(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def probe_exit_ip(port: int, timeout: float = 15.0) -> str | None:
    proxy = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
    request = urllib.request.Request(
        PROBE_URL, headers={"User-Agent": "crawl-hadith/0.1"}
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            value = response.read().decode("ascii", "ignore").strip()
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        return None
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return None
    return value


def _state_path(root: Path) -> Path:
    return root / "state.json"


def _read_state(root: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _write_state(root: Path, state: list[dict[str, Any]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _state_path(root).write_text(json.dumps(state, indent=2), encoding="utf-8")


def _is_pool_instance(pid: int, appdata: str) -> bool:
    """Only kill a PID whose command line still points at our appdata copy."""
    script = (
        f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}')"
        ".CommandLine"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    command = (result.stdout or "").strip().lower()
    if not command:
        return False
    return "nekoray.exe" in command and appdata.lower() in command


def stop_instances(root: Path, *, quiet: bool = False) -> int:
    state = _read_state(root)
    if not state:
        if not quiet:
            print(f"no running pool recorded in {root}")
        return 0
    stopped = 0
    for item in state:
        pid = int(item.get("pid") or 0)
        appdata = str(item.get("appdata") or "")
        if pid <= 0 or not appdata or not _is_pool_instance(pid, appdata):
            continue
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            stopped += 1
    state_path = _state_path(root)
    if state_path.exists():
        state_path.unlink()
    if not quiet:
        print(f"stopped {stopped} NekoRay instance(s)")
    return 0


def list_profiles(args: argparse.Namespace) -> int:
    source = Path(args.nekoray_dir) / "config"
    profiles = load_profiles(source)
    if not profiles:
        print(f"no profiles found in {source}", file=sys.stderr)
        return 1
    for profile_id, data in sorted(profiles.items()):
        print(_safe(profile_label(profile_id, data)))
    return 0


def _task_cmd(root: Path, args: argparse.Namespace) -> Path:
    script = Path(__file__).resolve()
    profiles = ",".join(str(pid) for pid in args.profiles)
    path = root / "run-pool.cmd"
    path.write_text(
        "@echo off\r\n"
        f'"{sys.executable}" "{script}" start --profiles "{profiles}" '
        f'--nekoray-dir "{Path(args.nekoray_dir).resolve()}" '
        f'--root "{root.resolve()}" --out "{Path(args.out).resolve()}"\r\n',
        encoding="utf-8",
    )
    return path


def start_instances(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        print("NekoRay pool is Windows-only", file=sys.stderr)
        return 1
    nekoray_dir = Path(args.nekoray_dir)
    exe = nekoray_dir / "nekoray.exe"
    source = nekoray_dir / "config"
    if not exe.exists() or not source.is_dir():
        print(f"NekoRay not found at {nekoray_dir}", file=sys.stderr)
        return 1
    if not is_admin():
        if args.elevate:
            return elevate()
        print(
            "NekoRay needs administrator rights. Run from an elevated terminal "
            "or add --elevate (one UAC prompt for the whole batch).",
            file=sys.stderr,
        )
        return 1

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    stop_instances(root, quiet=True)

    profiles = load_profiles(source)
    selected = list(dict.fromkeys(args.profiles))
    missing = [pid for pid in selected if pid not in profiles]
    if missing:
        print(f"profiles not found: {missing}", file=sys.stderr)
        return 1

    needed: list[int] = []
    for index in range(len(selected)):
        needed.extend((args.base_port + 2 * index, args.base_port + 2 * index + 1))
    busy = [port for port in needed if not port_free(port)]
    if busy:
        print(f"ports already in use: {busy}; try --base-port", file=sys.stderr)
        return 1

    state: list[dict[str, Any]] = []
    for index, profile_id in enumerate(selected):
        socks_port = args.base_port + 2 * index
        http_port = socks_port + 1
        base = root / f"profile-{profile_id}"
        try:
            prepare_instance(
                source, base / "config", profile_id, socks_port, http_port
            )
            proc = subprocess.Popen(
                [str(exe), "-many", "-appdata", str(base), "-tray"],
                cwd=str(nekoray_dir),
                close_fds=True,
            )
        except (OSError, ValueError) as exc:
            print(f"instance {profile_id} failed: {exc}", file=sys.stderr)
            continue
        state.append(
            {
                "profile": profile_id,
                "pid": proc.pid,
                "socks_port": socks_port,
                "http_port": http_port,
                "appdata": str(base),
                "exit_ip": None,
            }
        )
        name = str((profiles[profile_id].get("bean") or {}).get("name") or "")
        print(
            f"profile {profile_id} -> http://127.0.0.1:{http_port} "
            f"({_safe(name)})"
        )

    if not state:
        return 1
    _write_state(root, state)

    for item in state:
        port = int(item["http_port"])
        if not wait_port(port, args.wait):
            print(
                f"profile {item['profile']}: port {port} did not open",
                file=sys.stderr,
            )
            continue
        if not args.no_probe:
            item["exit_ip"] = probe_exit_ip(port)
            print(
                f"profile {item['profile']}: port {port}, exit "
                f"{item['exit_ip'] or 'unreachable'}"
            )
    _write_state(root, state)

    usable = [item for item in state if args.no_probe or item.get("exit_ip")]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "".join(f"http://127.0.0.1:{item['http_port']}\n" for item in usable),
        encoding="utf-8",
    )
    print(f"wrote {len(usable)} proxy line(s) to {out}")

    exits = [str(item["exit_ip"]) for item in usable if item.get("exit_ip")]
    duplicates = sorted({ip for ip in exits if exits.count(ip) > 1})
    if duplicates:
        print(
            f"warning: duplicate exit IP(s) {duplicates}; they share one rate limit",
            file=sys.stderr,
        )

    if args.print_task:
        path = _task_cmd(root, args)
        print("\nZero-prompt startup (run once in an elevated terminal):")
        print(
            subprocess.list2cmdline(
                [
                    "schtasks",
                    "/Create",
                    "/TN",
                    "NekoRayPool",
                    "/TR",
                    str(path),
                    "/SC",
                    "ONLOGON",
                    "/RL",
                    "HIGHEST",
                    "/F",
                ]
            )
        )
        print(f"task wrapper: {path}")
    return 0 if usable else 1


def stop_command(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        print("NekoRay pool is Windows-only", file=sys.stderr)
        return 1
    if not is_admin():
        if args.elevate:
            return elevate()
        print(
            "stopping NekoRay needs administrator rights; add --elevate.",
            file=sys.stderr,
        )
        return 1
    return stop_instances(Path(args.root))


def _profile_ids(value: str) -> list[int]:
    try:
        profiles = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid profile list: {value}") from exc
    if not profiles:
        raise argparse.ArgumentTypeError("profile list is empty")
    return profiles


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m crawl_hadith.nekoray_pool",
        description="Run one isolated NekoRay instance per proxy exit.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="list NekoRay profiles")
    list_parser.add_argument("--nekoray-dir", default=str(DEFAULT_NEKORAY_DIR))

    start_parser = sub.add_parser("start", help="start the proxy pool")
    start_parser.add_argument(
        "--profiles",
        type=_profile_ids,
        default=DEFAULT_PROFILES,
        help="comma-separated profile IDs (default: 0,3,6,22)",
    )
    start_parser.add_argument(
        "--base-port",
        type=int,
        default=DEFAULT_BASE_PORT,
        help="first local port; each instance uses base+2i and base+2i+1",
    )
    start_parser.add_argument("--root", default=str(_default_root()))
    start_parser.add_argument(
        "--out", default="proxies.txt", help="proxy list for --proxies-file"
    )
    start_parser.add_argument(
        "--wait", type=float, default=20.0, help="seconds to wait for each port"
    )
    start_parser.add_argument("--no-probe", action="store_true")
    start_parser.add_argument(
        "--print-task",
        action="store_true",
        help="print the one-time schtasks command for zero-prompt startup",
    )
    start_parser.add_argument("--elevate", action="store_true")
    start_parser.add_argument("--nekoray-dir", default=str(DEFAULT_NEKORAY_DIR))

    stop_parser = sub.add_parser(
        "stop", help="stop instances started by this helper"
    )
    stop_parser.add_argument("--root", default=str(_default_root()))
    stop_parser.add_argument("--elevate", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return list_profiles(args)
        if args.command == "start":
            return start_instances(args)
        if args.command == "stop":
            return stop_command(args)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
