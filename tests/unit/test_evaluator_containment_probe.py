from __future__ import annotations

import errno
import hashlib
import json
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import guildmind.sandbox.containment_probe as containment_contract

_REPOSITORY_ROOT = Path(__file__).parents[2]
_PROBE_PATH = _REPOSITORY_ROOT / "containers" / "evaluator" / "containment_probe.py"


def _load_probe() -> dict[str, Any]:
    return runpy.run_path(str(_PROBE_PATH))


def test_command_surface_is_closed_and_does_not_echo_values(capsys: Any) -> None:
    main = cast(Callable[[list[str]], int], _load_probe()["main"])

    assert main(["containment_probe.py", "arbitrary-profile"]) == 2
    assert main(["containment_probe.py", "candidate", "/caller/path"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("usage: containment_probe.py {candidate|scorer}\n") == 2
    assert "arbitrary-profile" not in captured.err
    assert "/caller/path" not in captured.err


def test_sentinel_scan_is_hash_only_complete_and_canonical(tmp_path: Path) -> None:
    sentinel = b"prefix guildmind-containment-v1:secret-value suffix\n"
    secret = tmp_path / "secret.txt"
    secret.write_bytes(sentinel)
    (tmp_path / "ordinary.txt").write_text("ordinary\n", encoding="ascii")
    scan = cast(Callable[[Path], dict[str, object]], _load_probe()["_scan_sentinels"])

    result = scan(tmp_path)

    assert result["file_sha256"] == [hashlib.sha256(sentinel).hexdigest()]
    assert result["files_examined"] == 2
    assert result["scan_errors"] == 0
    assert result["scan_truncated"] is False
    assert b"secret-value" not in json.dumps(result, sort_keys=True).encode("ascii")


def test_mountinfo_ignores_host_sources_and_hashes_only_unexpected_inputs() -> None:
    parse = cast(
        Callable[[bytes], tuple[dict[str, bool], bool, list[str]]],
        _load_probe()["_parse_mountinfo"],
    )
    data = (
        b"31 20 0:25 / /inputs/workspace ro,nosuid - ext4 /host/private/workspace rw\n"
        b"32 20 0:26 / /inputs/challenge.json ro - ext4 /host/private/challenge rw\n"
        b"33 20 0:27 / /inputs/hidden ro - ext4 /host/private/secret rw\n"
    )

    mounts, complete, unexpected = parse(data)

    assert complete is True
    assert mounts["/inputs/workspace"] is True
    assert mounts["/inputs/challenge.json"] is True
    assert unexpected == [hashlib.sha256(b"/inputs/hidden").hexdigest()]
    encoded = json.dumps([mounts, complete, unexpected], sort_keys=True)
    assert "/host/private" not in encoded


def test_active_write_detects_writable_file_and_directory_without_residue(
    tmp_path: Path,
) -> None:
    active_write = cast(Callable[..., tuple[str, int | None]], _load_probe()["_active_write"])
    regular = tmp_path / "input.txt"
    regular.write_text("unchanged\n", encoding="ascii")

    assert active_write(regular, directory=False) == ("succeeded", None)
    assert active_write(tmp_path, directory=True) == ("succeeded", None)
    assert regular.read_text(encoding="ascii") == "unchanged\n"
    assert not (tmp_path / ".guildmind-containment-write-probe").exists()


def test_tcp_observation_preserves_the_raw_connect_errno(monkeypatch: Any) -> None:
    namespace = _load_probe()

    class FakeSocket:
        def settimeout(self, timeout: float) -> None:
            assert timeout == namespace["_CONNECT_TIMEOUT_SECONDS"]

        def connect_ex(self, address: tuple[str, int]) -> int:
            assert address == ("169.254.169.254", 80)
            return errno.EHOSTUNREACH

        def close(self) -> None:
            pass

    probe = cast(Callable[..., dict[str, object]], namespace["_tcp_observation"])
    monkeypatch.setattr(probe.__globals__["socket"], "socket", lambda *args: FakeSocket())

    assert probe("metadata_aws", namespace["socket"].AF_INET, "169.254.169.254", 80) == {
        "errno": errno.EHOSTUNREACH,
        "outcome": "os_error",
        "target": "metadata_aws",
    }


def test_tcp_matrix_includes_fixed_non_dns_host_endpoints() -> None:
    targets = _load_probe()["_TCP_TARGETS"]

    assert [(target, address, port) for target, _, address, port in targets] == [
        ("external_ipv4", "93.184.216.34", 443),
        ("external_ipv6", "2606:4700:4700::1111", 443),
        ("metadata_aws", "169.254.169.254", 80),
        ("metadata_ecs", "169.254.170.2", 80),
        ("metadata_alibaba", "100.100.100.200", 80),
        ("host_docker_desktop", "192.168.65.2", 80),
        ("host_default_bridge", "172.17.0.1", 2375),
        ("loopback_ssh", "127.0.0.1", 22),
        ("loopback_http", "127.0.0.1", 80),
        ("loopback_docker", "127.0.0.1", 2375),
        ("loopback_docker_tls", "127.0.0.1", 2376),
    ]


def test_effective_environment_hashes_names_and_values_without_values() -> None:
    inventory = cast(
        Callable[[dict[str, str]], list[dict[str, str]]],
        _load_probe()["_environment_inventory"],
    )

    result = inventory({"ZED": "private-z", "ALPHA": "private-a"})

    assert result == [
        {"name": "ALPHA", "value_sha256": hashlib.sha256(b"private-a").hexdigest()},
        {"name": "ZED", "value_sha256": hashlib.sha256(b"private-z").hexdigest()},
    ]
    assert "private" not in json.dumps(result)


def test_route_and_interface_inventory_errors_are_explicit(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    namespace = _load_probe()
    routes = cast(Callable[[], tuple[list[str], bool]], namespace["_default_route_families"])
    interfaces = cast(
        Callable[[], tuple[list[str], bool]], namespace["_usable_non_loopback_interfaces"]
    )
    missing = tmp_path / "missing"
    routes.__globals__["_PROC_NET_ROUTE_PATH"] = missing
    routes.__globals__["_PROC_NET_IPV6_ROUTE_PATH"] = missing
    interfaces.__globals__["_PROC_NET_IF_INET6_PATH"] = missing
    monkeypatch.setattr(interfaces.__globals__["socket"], "if_nameindex", lambda: [])

    assert routes() == ([], True)
    assert interfaces() == ([], True)


def test_no_network_ipv6_reject_route_is_not_a_default_route(tmp_path: Path) -> None:
    namespace = _load_probe()
    routes = cast(Callable[[], tuple[list[str], bool]], namespace["_default_route_families"])
    ipv4 = tmp_path / "route"
    ipv6 = tmp_path / "ipv6_route"
    ipv4.write_text(
        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n",
        encoding="ascii",
    )
    ipv6.write_text(
        f"{'0' * 32} 00 {'0' * 32} 00 {'0' * 32} ffffffff 00000001 00000000 00200200 lo\n",
        encoding="ascii",
    )
    routes.__globals__["_PROC_NET_ROUTE_PATH"] = ipv4
    routes.__globals__["_PROC_NET_IPV6_ROUTE_PATH"] = ipv6

    assert routes() == ([], False)


def test_ipv4_route_inventory_requires_the_complete_ordered_header(tmp_path: Path) -> None:
    namespace = _load_probe()
    routes = cast(Callable[[], tuple[list[str], bool]], namespace["_default_route_families"])
    ipv4 = tmp_path / "route"
    ipv6 = tmp_path / "ipv6_route"
    ipv4.write_text("garbage\n", encoding="ascii")
    ipv6.write_text("", encoding="ascii")
    routes.__globals__["_PROC_NET_ROUTE_PATH"] = ipv4
    routes.__globals__["_PROC_NET_IPV6_ROUTE_PATH"] = ipv6

    assert routes() == ([], True)


def test_ipv4_interface_probe_does_not_hide_unexpected_ioctl_errors(
    monkeypatch: Any,
) -> None:
    namespace = _load_probe()
    probe = cast(Callable[[str], bool], namespace["_interface_has_ipv4"])

    class FakeSocket:
        def fileno(self) -> int:
            return 7

        def close(self) -> None:
            pass

    monkeypatch.setattr(probe.__globals__["socket"], "socket", lambda *args: FakeSocket())

    def denied(*args: object) -> bytes:
        raise OSError(errno.EPERM, "denied")

    monkeypatch.setattr(probe.__globals__["fcntl"], "ioctl", denied)

    with pytest.raises(RuntimeError, match="IPv4 interface address was unavailable"):
        probe("eth0")


def test_fixed_credential_targets_include_the_effective_home() -> None:
    targets = dict(_load_probe()["_CREDENTIAL_TARGETS"])

    assert targets["home_nonroot_aws"] == Path("/.aws")
    assert targets["home_nonroot_gcloud"] == Path("/.config/gcloud")
    assert targets["home_nonroot_azure"] == Path("/.azure")


def test_fixed_observation_conforms_to_the_host_model_and_ascii_output(
    capsys: Any,
) -> None:
    namespace = _load_probe()
    main = cast(Callable[[list[str]], int], namespace["main"])
    program_hash = hashlib.sha256(_PROBE_PATH.read_bytes()).hexdigest()
    environment = [{"name": "LANG", "value_sha256": hashlib.sha256(b"C").hexdigest()}]
    mounts = [
        {
            "present": target in {"workspace", "challenge"},
            "read_only": True if target in {"workspace", "challenge"} else None,
            "target": target,
            "write_errno": errno.EROFS if target in {"workspace", "challenge"} else None,
            "write_outcome": "denied" if target in {"workspace", "challenge"} else "absent",
        }
        for target in ("workspace", "challenge", "grader", "response")
    ]
    credentials = [
        {"errno": None, "outcome": "absent", "readable": False, "target": target}
        for target in (
            "run_secrets",
            "kubernetes_serviceaccount",
            "root_aws",
            "root_gcloud",
            "root_azure",
            "home_nonroot_aws",
            "home_nonroot_gcloud",
            "home_nonroot_azure",
        )
    ]
    network = {
        "default_route_families": [],
        "dns": [
            {"error_code": socket_code, "outcome": "gai_error", "target": target}
            for target, socket_code in zip(
                namespace["_DNS_TARGETS"],
                (-3, -3, -3, -3, -3),
                strict=True,
            )
        ],
        "interface_scan_error": False,
        "packet_socket": {"errno": errno.EPERM, "outcome": "os_error"},
        "proc_net_unix_entries": 0,
        "proc_net_unix_scan_error": False,
        "raw_socket": {"errno": errno.EPERM, "outcome": "os_error"},
        "route_scan_error": False,
        "socket_inventory": [
            {"root": root, "scan_error": False, "socket_count": 0}
            for root, _ in namespace["_SOCKET_ROOTS"]
        ],
        "tcp": [
            {
                "errno": errno.ECONNREFUSED
                if target.startswith("loopback_")
                else errno.ENETUNREACH,
                "outcome": "os_error",
                "target": target,
            }
            for target, _, _, _ in namespace["_TCP_TARGETS"]
        ],
        "unix": [
            {
                "errno": None,
                "is_socket": False,
                "outcome": "absent",
                "present": False,
                "target": target,
            }
            for target, _ in namespace["_UNIX_TARGETS"]
        ],
        "usable_non_loopback_interfaces": [],
    }
    observation = {
        "credentials": credentials,
        "environment": environment,
        "mountinfo_complete": True,
        "mounts": mounts,
        "network": network,
        "profile": "candidate",
        "program_sha256": program_hash,
        "schema_version": "guildmind.containment-probe/v1",
        "sentinels": {
            "environment": [],
            "file_sha256": [],
            "files_examined": 0,
            "scan_errors": 0,
            "scan_truncated": False,
        },
        "unexpected_input_mounts": [],
    }
    main.__globals__["_collect_observation"] = lambda profile: {
        **observation,
        "profile": profile,
    }

    assert main(["containment_probe.py", "candidate"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.endswith("\n")
    assert captured.out.count("\n") == 1
    assert captured.out.isascii()
    parsed = containment_contract.ContainmentObservation.model_validate_json(captured.out)
    assert parsed.profile is containment_contract.ContainmentProfile.CANDIDATE
    assert parsed.program_sha256 == program_hash
    assert tuple(item.target for item in parsed.network.dns) == containment_contract._DNS_ORDER
    assert tuple(item.target for item in parsed.network.tcp) == containment_contract._TCP_ORDER
    assert tuple(item.target for item in parsed.network.unix) == containment_contract._UNIX_ORDER


def test_program_is_image_owned_and_self_hash_is_exact() -> None:
    dockerfile = (_REPOSITORY_ROOT / "containers" / "evaluator" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    dockerignore = (_REPOSITORY_ROOT / "containers" / "evaluator" / ".dockerignore").read_text(
        encoding="utf-8"
    )
    namespace = _load_probe()
    program_hash = cast(Callable[[], str], namespace["_program_sha256"])

    assert (
        "COPY --chown=0:0 --chmod=0555 containment_probe.py /opt/guildmind/containment_probe.py"
    ) in dockerfile
    assert "!containment_probe.py" in dockerignore.splitlines()
    assert program_hash() == hashlib.sha256(_PROBE_PATH.read_bytes()).hexdigest()
