from copy import deepcopy

from traforo_acceptance import host, runner


def test_linux_native_stage_never_selects_macos_host_tests(run_root, monkeypatch):
    monkeypatch.setattr(runner.platform, "system", lambda: "Linux")
    commands = runner.stage_commands("native", run_root, {})
    argv, _ = commands[0]
    assert "--integration-linux-privileged" in argv
    assert "--integration-macos-privileged" not in argv
    assert "tests/integration/test_tunnel_linux.py" in argv


def test_linux_independent_audit_rejects_dns_and_route_changes():
    before = {
        "platform": "linux",
        "interfaces": [[1, "lo"], [2, "eth0"]],
        "routes": [{"dst": "default", "dev": "eth0", "gateway": "192.168.5.2"}],
        "dns": {"DNSEx": [[2, 2, [192, 168, 5, 2], 53, ""]], "Domains": []},
        "resolv_conf": "/run/systemd/resolve/stub-resolv.conf",
        "helper": None,
        "service": None,
        "processes": "PID PPID UID COMM\n",
        "administration_remnants": [],
        "systemd": {"exit_code": 3, "output": "inactive"},
        "state_exists": False,
    }
    assert all(host.compare(before, deepcopy(before)).values())
    after = deepcopy(before)
    after["dns"]["Domains"] = [[9, "private.test", True]]
    after["routes"].append({"dst": "10.254.9.0/24", "dev": "stale-tun"})
    checks = host.compare(before, after)
    assert checks["dns"] is False
    assert checks["routes"] is False
