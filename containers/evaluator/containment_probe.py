"""Fixed, image-owned active probes for evaluator containment.

Callers may select only the candidate- or scorer-shaped profile.  The probe emits
hashes and bounded inventories rather than secret values, host mount sources, or
dynamic socket paths.  The host owns every expectation and derives every verdict.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import ipaddress
import json
import os
import queue
import socket
import stat
import struct
import sys
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path

_SCHEMA_VERSION = "guildmind.containment-probe/v1"
_SENTINEL_PREFIX = b"guildmind-containment-v1:"
_OUTPUT_LIMIT = 12_288

_INPUT_ROOT = Path("/inputs")
_MOUNTINFO_PATH = Path("/proc/self/mountinfo")
_PROC_NET_ROUTE_PATH = Path("/proc/net/route")
_PROC_NET_IPV6_ROUTE_PATH = Path("/proc/net/ipv6_route")
_PROC_NET_IF_INET6_PATH = Path("/proc/net/if_inet6")
_PROC_NET_UNIX_PATH = Path("/proc/net/unix")
_SYS_CLASS_NET = Path("/sys/class/net")

_MOUNTINFO_LIMIT = 262_144
_PROC_READ_LIMIT = 262_144
_SENTINEL_MAX_FILES = 256
_SENTINEL_MAX_ENTRIES = 1_024
_SENTINEL_MAX_FILE_BYTES = 1_048_576
_SENTINEL_MAX_TOTAL_BYTES = 8_388_608
_SOCKET_SCAN_MAX_ENTRIES = 2_048
_SOCKET_SCAN_MAX_DIRECTORIES = 256
_DNS_TIMEOUT_SECONDS = 0.25
_CONNECT_TIMEOUT_SECONDS = 0.25

_IPV4_ROUTE_HEADER = (
    "Iface",
    "Destination",
    "Gateway",
    "Flags",
    "RefCnt",
    "Use",
    "Metric",
    "Mask",
    "MTU",
    "Window",
    "IRTT",
)

_MOUNTS = (
    ("workspace", Path("/inputs/workspace")),
    ("challenge", Path("/inputs/challenge.json")),
    ("grader", Path("/inputs/grader")),
    ("response", Path("/inputs/response.txt")),
)
_EXPECTED_INPUT_MOUNTS = frozenset(str(path) for _, path in _MOUNTS)

_DNS_TARGETS = (
    "example.com",
    "pypi.org",
    "host.docker.internal",
    "gateway.docker.internal",
    "kubernetes.default.svc",
)
_TCP_TARGETS = (
    ("external_ipv4", socket.AF_INET, "93.184.216.34", 443),
    ("external_ipv6", socket.AF_INET6, "2606:4700:4700::1111", 443),
    ("metadata_aws", socket.AF_INET, "169.254.169.254", 80),
    ("metadata_ecs", socket.AF_INET, "169.254.170.2", 80),
    ("metadata_alibaba", socket.AF_INET, "100.100.100.200", 80),
    ("host_docker_desktop", socket.AF_INET, "192.168.65.2", 80),
    ("host_default_bridge", socket.AF_INET, "172.17.0.1", 2375),
    ("loopback_ssh", socket.AF_INET, "127.0.0.1", 22),
    ("loopback_http", socket.AF_INET, "127.0.0.1", 80),
    ("loopback_docker", socket.AF_INET, "127.0.0.1", 2375),
    ("loopback_docker_tls", socket.AF_INET, "127.0.0.1", 2376),
)
_UNIX_TARGETS = (
    ("docker_var_run", Path("/var/run/docker.sock")),
    ("docker_run", Path("/run/docker.sock")),
    ("containerd", Path("/run/containerd/containerd.sock")),
    ("podman", Path("/run/podman/podman.sock")),
    ("crio", Path("/var/run/crio/crio.sock")),
    ("ssh_auth_sock", Path("/run/ssh-agent.sock")),
)
_SOCKET_ROOTS = (
    ("run", Path("/run")),
    ("var_run", Path("/var/run")),
    ("tmp", Path("/tmp")),
    ("workspace", Path("/workspace")),
    ("inputs", Path("/inputs")),
)
_CREDENTIAL_TARGETS = (
    ("run_secrets", Path("/run/secrets")),
    (
        "kubernetes_serviceaccount",
        Path("/var/run/secrets/kubernetes.io/serviceaccount"),
    ),
    ("root_aws", Path("/root/.aws")),
    ("root_gcloud", Path("/root/.config/gcloud")),
    ("root_azure", Path("/root/.azure")),
    ("home_nonroot_aws", Path("/.aws")),
    ("home_nonroot_gcloud", Path("/.config/gcloud")),
    ("home_nonroot_azure", Path("/.azure")),
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _program_sha256() -> str:
    return _sha256(Path(__file__).read_bytes())


def _canonical_json(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("ascii")) + 1 > _OUTPUT_LIMIT:
        raise RuntimeError("containment probe output exceeded its fixed bound")
    return encoded


def _emit(payload: Mapping[str, object]) -> None:
    encoded = _canonical_json(payload)
    sys.stdout.write(f"{encoded}\n")
    sys.stdout.flush()


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("fixed probe input exceeded its read bound")
    return data


def _scan_sentinel_file(path: Path, remaining: int) -> tuple[str | None, int, bool]:
    allowed = min(_SENTINEL_MAX_FILE_BYTES, remaining)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        data = bytearray()
        while len(data) <= allowed:
            chunk = os.read(descriptor, min(65_536, allowed + 1 - len(data)))
            if not chunk:
                raw = bytes(data)
                return (_sha256(raw) if _SENTINEL_PREFIX in raw else None, len(raw), False)
            data.extend(chunk)
        return None, allowed, True
    finally:
        os.close(descriptor)


def _scan_sentinels(root: Path = _INPUT_ROOT) -> dict[str, object]:
    hashes: set[str] = set()
    files_examined = 0
    entries_examined = 0
    bytes_examined = 0
    scan_errors = 0
    scan_truncated = False
    pending = [root]

    while pending and not scan_truncated:
        current = pending.pop()
        entries: tuple[Path, ...]
        try:
            if current.is_file():
                entries = (current,)
            elif current.is_dir():
                with os.scandir(current) as iterator:
                    bounded_entries: list[Path] = []
                    for directory_entry in iterator:
                        bounded_entries.append(Path(directory_entry.path))
                        if len(bounded_entries) > _SENTINEL_MAX_ENTRIES:
                            scan_truncated = True
                            break
                    entries = tuple(bounded_entries)
            else:
                entries = ()
                scan_errors += 1
        except OSError:
            scan_errors += 1
            continue

        for entry_path in entries:
            entries_examined += 1
            if entries_examined > _SENTINEL_MAX_ENTRIES:
                scan_truncated = True
                break
            try:
                metadata = entry_path.stat(follow_symlinks=False)
            except OSError:
                scan_errors += 1
                continue
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(entry_path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            if files_examined >= _SENTINEL_MAX_FILES:
                scan_truncated = True
                break
            remaining = _SENTINEL_MAX_TOTAL_BYTES - bytes_examined
            if remaining <= 0:
                scan_truncated = True
                break
            files_examined += 1
            try:
                digest, consumed, truncated = _scan_sentinel_file(entry_path, remaining)
            except OSError:
                scan_errors += 1
                continue
            bytes_examined += consumed
            scan_truncated = scan_truncated or truncated
            if digest is not None:
                hashes.add(digest)

    sentinel_environment = [
        {"name": name, "value_sha256": _sha256(value.encode("utf-8"))}
        for name, value in sorted(os.environ.items())
        if _SENTINEL_PREFIX in value.encode("utf-8")
    ]
    return {
        "environment": sentinel_environment,
        "file_sha256": sorted(hashes),
        "files_examined": files_examined,
        "scan_errors": scan_errors,
        "scan_truncated": scan_truncated,
    }


def _environment_inventory(
    environment: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    values = os.environ if environment is None else environment
    return [
        {"name": name, "value_sha256": _sha256(value.encode("utf-8"))}
        for name, value in sorted(values.items())
    ]


def _decode_mount_field(value: str) -> str:
    decoded = bytearray()
    index = 0
    raw = value.encode("ascii")
    escapes = {b"040": b" ", b"011": b"\t", b"012": b"\n", b"134": b"\\"}
    while index < len(raw):
        if raw[index] != 92:
            decoded.append(raw[index])
            index += 1
            continue
        code = raw[index + 1 : index + 4]
        replacement = escapes.get(code)
        if replacement is None:
            raise ValueError("mountinfo contained an invalid escape")
        decoded.extend(replacement)
        index += 4
    return decoded.decode("utf-8")


def _parse_mountinfo(data: bytes) -> tuple[dict[str, bool], bool, list[str]]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return {}, False, []
    mounts: dict[str, bool] = {}
    unexpected: set[str] = set()
    complete = bool(text.endswith("\n"))
    for line in text.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        if not separator or len(fields) < 6 or len(after.split()) < 3:
            complete = False
            continue
        try:
            mountpoint = _decode_mount_field(fields[4])
        except (UnicodeDecodeError, ValueError):
            complete = False
            continue
        if mountpoint in mounts:
            complete = False
        mounts[mountpoint] = "ro" in fields[5].split(",")
        if (mountpoint == "/inputs" or mountpoint.startswith("/inputs/")) and (
            mountpoint not in _EXPECTED_INPUT_MOUNTS
        ):
            unexpected.add(_sha256(mountpoint.encode("utf-8")))
    return mounts, complete, sorted(unexpected)


def _mountinfo() -> tuple[dict[str, bool], bool, list[str]]:
    try:
        data = _read_bounded(_MOUNTINFO_PATH, _MOUNTINFO_LIMIT)
    except (OSError, RuntimeError):
        return {}, False, []
    return _parse_mountinfo(data)


def _active_write(path: Path, *, directory: bool) -> tuple[str, int | None]:
    descriptor: int | None = None
    created: Path | None = None
    try:
        if directory:
            created = path / ".guildmind-containment-write-probe"
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(created, flags, 0o600)
        else:
            flags = (
                os.O_WRONLY
                | os.O_APPEND
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(path, flags)
    except OSError as error:
        outcome = (
            "denied" if error.errno in {errno.EPERM, errno.EACCES, errno.EROFS} else "os_error"
        )
        return outcome, error.errno if error.errno is not None else errno.EIO
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created is not None:
            with suppress(OSError):
                created.unlink()
    return "succeeded", None


def _mount_observations() -> tuple[list[dict[str, object]], bool, list[str]]:
    mountinfo, complete, unexpected = _mountinfo()
    observations: list[dict[str, object]] = []
    for target, path in _MOUNTS:
        try:
            metadata = path.stat()
        except OSError:
            observations.append(
                {
                    "present": False,
                    "read_only": None,
                    "target": target,
                    "write_errno": None,
                    "write_outcome": "absent",
                }
            )
            continue
        path_value = str(path)
        if path_value not in mountinfo:
            complete = False
        outcome, write_errno = _active_write(path, directory=stat.S_ISDIR(metadata.st_mode))
        observations.append(
            {
                "present": True,
                "read_only": mountinfo.get(path_value, False),
                "target": target,
                "write_errno": write_errno,
                "write_outcome": outcome,
            }
        )
    return observations, complete, unexpected


def _credential_observation(target: str, path: Path) -> dict[str, object]:
    try:
        path.stat()
    except OSError as error:
        code = error.errno if error.errno is not None else errno.EIO
        if code in {errno.ENOENT, errno.ENOTDIR}:
            return {"errno": None, "outcome": "absent", "readable": False, "target": target}
        if code in {errno.EPERM, errno.EACCES}:
            return {
                "errno": code,
                "outcome": "inaccessible",
                "readable": False,
                "target": target,
            }
        return {"errno": code, "outcome": "os_error", "readable": False, "target": target}
    try:
        readable = os.access(path, os.R_OK, effective_ids=True)
    except TypeError:
        readable = os.access(path, os.R_OK)
    return {"errno": None, "outcome": "present", "readable": readable, "target": target}


def _credential_observations() -> list[dict[str, object]]:
    return [_credential_observation(target, path) for target, path in _CREDENTIAL_TARGETS]


def _interface_has_ipv4(name: str) -> bool:
    descriptor = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        request = struct.pack("256s", name.encode("ascii")[:15])
        response = fcntl.ioctl(descriptor.fileno(), 0x8915, request)  # SIOCGIFADDR
    except OSError as error:
        if error.errno == errno.EADDRNOTAVAIL:
            return False
        raise RuntimeError("IPv4 interface address was unavailable") from error
    except UnicodeEncodeError as error:
        raise RuntimeError("network interface name was not ASCII") from error
    finally:
        descriptor.close()
    try:
        address = ipaddress.ip_address(response[20:24])
    except ValueError as error:
        raise RuntimeError("IPv4 interface address was malformed") from error
    return not address.is_loopback and not address.is_unspecified


def _ipv6_addressed_interfaces() -> tuple[set[str], bool]:
    try:
        data = _read_bounded(_PROC_NET_IF_INET6_PATH, _PROC_READ_LIMIT).decode("ascii")
    except (OSError, RuntimeError, UnicodeDecodeError):
        return set(), True
    result: set[str] = set()
    scan_error = bool(data and not data.endswith("\n"))
    for line in data.splitlines():
        fields = line.split()
        if len(fields) != 6:
            scan_error = True
            continue
        try:
            address = ipaddress.ip_address(bytes.fromhex(fields[0]))
        except ValueError:
            scan_error = True
            continue
        if not address.is_loopback and not address.is_unspecified:
            result.add(fields[5])
    return result, scan_error


def _usable_non_loopback_interfaces() -> tuple[list[str], bool]:
    ipv6, scan_error = _ipv6_addressed_interfaces()
    usable: set[str] = set()
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return [], True
    for _, name in interfaces:
        try:
            flags = int((_SYS_CLASS_NET / name / "flags").read_text(encoding="ascii").strip(), 0)
        except (OSError, ValueError):
            scan_error = True
            continue
        if not flags & 0x1 or flags & 0x8:  # IFF_UP, IFF_LOOPBACK
            continue
        try:
            has_ipv4 = _interface_has_ipv4(name)
        except RuntimeError:
            has_ipv4 = False
            scan_error = True
        if has_ipv4 or name in ipv6:
            usable.add(name)
    return sorted(usable), scan_error


def _default_route_families() -> tuple[list[str], bool]:
    families: list[str] = []
    scan_error = False
    try:
        ipv4 = _read_bounded(_PROC_NET_ROUTE_PATH, _PROC_READ_LIMIT).decode("ascii")
    except (OSError, RuntimeError, UnicodeDecodeError):
        ipv4 = ""
        scan_error = True
    ipv4_lines = ipv4.splitlines()
    if (
        not ipv4_lines
        or not ipv4.endswith("\n")
        or tuple(ipv4_lines[0].split()) != _IPV4_ROUTE_HEADER
    ):
        scan_error = True
    for line in ipv4_lines[1:]:
        fields = line.split()
        if len(fields) != len(_IPV4_ROUTE_HEADER):
            scan_error = True
            continue
        try:
            destination = int(fields[1], 16)
            flags = int(fields[3], 16)
            mask = int(fields[7], 16)
        except ValueError:
            scan_error = True
            continue
        if destination == 0 and mask == 0 and flags & 0x1:
            families.append("ipv4")
            break
    try:
        ipv6 = _read_bounded(_PROC_NET_IPV6_ROUTE_PATH, _PROC_READ_LIMIT).decode("ascii")
    except (OSError, RuntimeError, UnicodeDecodeError):
        ipv6 = ""
        scan_error = True
    if ipv6 and not ipv6.endswith("\n"):
        scan_error = True
    for line in ipv6.splitlines():
        fields = line.split()
        if len(fields) != 10:
            scan_error = True
            continue
        try:
            destination = int(fields[0], 16)
            prefix_length = int(fields[1], 16)
            flags = int(fields[8], 16)
        except ValueError:
            scan_error = True
            continue
        if destination == 0 and prefix_length == 0 and flags & 0x1:
            families.append("ipv6")
            break
    return families, scan_error


def _resolve_dns(target: str) -> dict[str, object]:
    result: queue.Queue[tuple[str, int | None]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            socket.getaddrinfo(target, 443, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            code = error.errno if error.errno is not None else socket.EAI_FAIL
            result.put(("gai_error", code))
        except OSError as error:
            result.put(("os_error", error.errno if error.errno is not None else errno.EIO))
        else:
            result.put(("resolved", None))

    threading.Thread(target=resolve, daemon=True).start()
    try:
        outcome, error_code = result.get(timeout=_DNS_TIMEOUT_SECONDS)
    except queue.Empty:
        outcome, error_code = "timeout", None
    return {"error_code": error_code, "outcome": outcome, "target": target}


def _tcp_observation(
    target: str,
    family: socket.AddressFamily,
    address: str,
    port: int,
) -> dict[str, object]:
    connection = socket.socket(family, socket.SOCK_STREAM)
    try:
        connection.settimeout(_CONNECT_TIMEOUT_SECONDS)
        code = connection.connect_ex((address, port))
    except TimeoutError:
        return {"errno": None, "outcome": "timeout", "target": target}
    except OSError as error:
        code = error.errno if error.errno is not None else errno.EIO
        return {"errno": code, "outcome": "os_error", "target": target}
    finally:
        connection.close()
    if code == 0:
        return {"errno": None, "outcome": "connected", "target": target}
    return {"errno": code, "outcome": "os_error", "target": target}


def _unix_observation(target: str, path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except OSError as error:
        if error.errno in {errno.ENOENT, errno.ENOTDIR}:
            return {
                "errno": None,
                "is_socket": False,
                "outcome": "absent",
                "present": False,
                "target": target,
            }
        return {
            "errno": error.errno if error.errno is not None else errno.EIO,
            "is_socket": True,
            "outcome": "os_error",
            "present": True,
            "target": target,
        }
    if not stat.S_ISSOCK(metadata.st_mode):
        return {
            "errno": None,
            "is_socket": False,
            "outcome": "not_socket",
            "present": True,
            "target": target,
        }
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(_CONNECT_TIMEOUT_SECONDS)
        connection.connect(str(path))
    except ConnectionRefusedError:
        outcome, code = "refused", None
    except TimeoutError:
        outcome, code = "timeout", None
    except OSError as error:
        outcome = "os_error"
        code = error.errno if error.errno is not None else errno.EIO
    else:
        outcome, code = "connected", None
    finally:
        connection.close()
    return {
        "errno": code,
        "is_socket": True,
        "outcome": outcome,
        "present": True,
        "target": target,
    }


def _socket_inventory(root: str, path: Path) -> dict[str, object]:
    count = 0
    entries_examined = 0
    directories_examined = 0
    scan_error = False
    pending = [path]
    while pending and not scan_error:
        current = pending.pop()
        directories_examined += 1
        if directories_examined > _SOCKET_SCAN_MAX_DIRECTORIES:
            scan_error = True
            break
        try:
            with os.scandir(current) as iterator:
                bounded_entries: list[os.DirEntry[str]] = []
                for entry in iterator:
                    bounded_entries.append(entry)
                    if len(bounded_entries) > _SOCKET_SCAN_MAX_ENTRIES:
                        scan_error = True
                        break
                entries = tuple(bounded_entries)
        except FileNotFoundError:
            continue
        except OSError:
            scan_error = True
            break
        for entry in entries:
            entries_examined += 1
            if entries_examined > _SOCKET_SCAN_MAX_ENTRIES:
                scan_error = True
                break
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                scan_error = True
                break
            if stat.S_ISSOCK(metadata.st_mode):
                count += 1
            elif stat.S_ISDIR(metadata.st_mode):
                pending.append(Path(entry.path))
    return {"root": root, "scan_error": scan_error, "socket_count": count}


def _proc_net_unix() -> tuple[int, bool]:
    try:
        data = _read_bounded(_PROC_NET_UNIX_PATH, _PROC_READ_LIMIT).decode("ascii")
    except (OSError, RuntimeError, UnicodeDecodeError):
        return 0, True
    lines = data.splitlines()
    if not lines or not lines[0].startswith("Num"):
        return 0, True
    return sum(bool(line.strip()) for line in lines[1:]), not data.endswith("\n")


def _privileged_socket(*, packet: bool) -> dict[str, object]:
    descriptor: socket.socket | None = None
    try:
        if packet:
            family = socket.__dict__.get("AF_PACKET")
            if not isinstance(family, int):
                raise OSError(errno.EAFNOSUPPORT, "packet sockets are unavailable")
            descriptor = socket.socket(family, socket.SOCK_RAW, socket.htons(3))
        else:
            descriptor = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    except (AttributeError, OSError) as error:
        code = getattr(error, "errno", None)
        return {"errno": code if code is not None else errno.EAFNOSUPPORT, "outcome": "os_error"}
    finally:
        if descriptor is not None:
            descriptor.close()
    return {"errno": None, "outcome": "created"}


def _network_observation() -> dict[str, object]:
    proc_entries, proc_error = _proc_net_unix()
    interfaces, interface_scan_error = _usable_non_loopback_interfaces()
    routes, route_scan_error = _default_route_families()
    return {
        "default_route_families": routes,
        "dns": [_resolve_dns(target) for target in _DNS_TARGETS],
        "interface_scan_error": interface_scan_error,
        "packet_socket": _privileged_socket(packet=True),
        "proc_net_unix_entries": proc_entries,
        "proc_net_unix_scan_error": proc_error,
        "raw_socket": _privileged_socket(packet=False),
        "route_scan_error": route_scan_error,
        "socket_inventory": [_socket_inventory(root, path) for root, path in _SOCKET_ROOTS],
        "tcp": [
            _tcp_observation(target, family, address, port)
            for target, family, address, port in _TCP_TARGETS
        ],
        "unix": [_unix_observation(target, path) for target, path in _UNIX_TARGETS],
        "usable_non_loopback_interfaces": interfaces,
    }


def _collect_observation(profile: str) -> dict[str, object]:
    mounts, mountinfo_complete, unexpected = _mount_observations()
    return {
        "credentials": _credential_observations(),
        "environment": _environment_inventory(),
        "mountinfo_complete": mountinfo_complete,
        "mounts": mounts,
        "network": _network_observation(),
        "profile": profile,
        "program_sha256": _program_sha256(),
        "schema_version": _SCHEMA_VERSION,
        "sentinels": _scan_sentinels(),
        "unexpected_input_mounts": unexpected,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv if argv is None else argv)
    if len(arguments) != 2 or arguments[1] not in {"candidate", "scorer"}:
        sys.stderr.write("usage: containment_probe.py {candidate|scorer}\n")
        return 2
    try:
        observation = _collect_observation(arguments[1])
        _emit(observation)
    except Exception:
        sys.stderr.write("containment probe failed\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
