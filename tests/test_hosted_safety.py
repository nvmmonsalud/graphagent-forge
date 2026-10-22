"""Offline protocol and subprocess egress checks for the opt-in hosted harness."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from hosted.provider import FixtureProvider

ROOT = Path(__file__).resolve().parents[1]


def completion(provider, content):
    request = Request(
        f"{provider.base_url}/chat/completions",
        data=json.dumps({"messages": [{"role": "user", "content": content}]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=3) as response:
        value = json.load(response)
    return value, json.loads(value["choices"][0]["message"]["content"])


def test_provider_protocol_and_exact_sent_citation(capsys):
    with FixtureProvider() as provider:
        value, graph = completion(provider, "<document>invented fixture</document>")
        assert len(graph["nodes"]) == 2
        assert graph["nodes"][0]["label"] == "Apricot"
        assert graph["edges"][0]["target"] == graph["nodes"][1]["id"]
        assert value["usage"] == {
            "prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60,
        }
        _, answer = completion(
            provider, '<source_excerpts>[{"id":"real-sent-id","text":"invented"}]'
            '</source_excerpts>\n\nQuestion: What did Apricot build?',
        )
        assert answer["citations"] == ["real-sent-id"]
        assert provider.calls == {"extraction": 1, "answer": 1}
        provider.reset()
        assert provider.calls == {"extraction": 0, "answer": 0}
    assert capsys.readouterr().err == ""


def test_provider_failure_is_nonretryable_and_malformed_ids_fail_closed():
    with FixtureProvider() as provider:
        provider.mode = "fail"
        with pytest.raises(HTTPError) as failed:
            completion(provider, "<document>synthetic</document>")
        assert failed.value.code == 400
        provider.mode = "ok"
        with pytest.raises(HTTPError) as malformed:
            completion(provider, "<source_excerpts>[]</source_excerpts>")
        assert malformed.value.code == 400
        assert provider.calls == {"extraction": 1, "answer": 0}


def test_provider_pause_releases_without_retaining_request():
    with FixtureProvider() as provider:
        provider.mode = "pause"
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(
            completion(provider, "<document>synthetic</document>")[1],
        ))
        thread.start()
        try:
            assert provider.entered.wait(2)
            assert not outcome
            provider.release.set()
            thread.join(timeout=2)
            assert outcome[0]["nodes"][0]["label"] == "Apricot"
        finally:
            provider.release.set()
            thread.join(timeout=2)


def run_guard_probe(tmp_path, source, port):
    report = tmp_path / "network.json"
    bootstrap = (
        "from pathlib import Path\n"
        "from hosted.network_guard import install_network_guard\n"
        f"install_network_guard(allowed_ports={{{port}}}, listen_port={port}, "
        f"report_path=Path({str(report)!r}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", bootstrap + source], cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""),
             "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "tests")))},
        capture_output=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b""
    return json.loads(report.read_text())


@pytest.mark.parametrize("operation", [
    "socket.socket().connect(('203.0.113.1', 443))",
    "socket.socket().connect(('127.0.0.1', 1))",
    "socket.getaddrinfo('api.moonshot.ai', 443)",
    "socket.gethostbyname('localhost')",
    "socket.socket().bind(('0.0.0.0', 8080))",
    "socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('127.0.0.1', 1))",
    "socket.socket(socket.AF_INET6).connect(('::1', 8080))",
])
def test_guard_denies_unapproved_network_without_disclosing_destination(tmp_path, operation):
    source = (
        "import socket\ntry:\n    " + operation
        + "\nexcept PermissionError:\n    pass\nelse:\n    raise AssertionError('not blocked')\n"
    )
    report = run_guard_probe(tmp_path, source, 59999)
    assert report == {"version": 1, "connect_allowed": 0, "bind_allowed": 0,
                      "dns_allowed": 0, "denied": 1}


def test_guard_allows_only_assigned_literal_service(tmp_path):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        report = run_guard_probe(tmp_path, (
            "import socket\n"
            f"socket.getaddrinfo('127.0.0.1', {port}, socket.AF_INET)\n"
            f"s=socket.create_connection(('127.0.0.1', {port}), timeout=1)\n"
            "s.close()\n"
        ), port)
        connection, _ = server.accept()
        connection.close()
    assert report["denied"] == 0
    assert report["connect_allowed"] == 1
    assert report["dns_allowed"] >= 1


def test_guard_allows_assigned_app_listener_and_asyncio_socketpair(tmp_path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    report = run_guard_probe(tmp_path, (
        "import socket, asyncio\n"
        f"s=socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(); s.close()\n"
        "async def run():\n    await asyncio.sleep(0)\n"
        "asyncio.run(run())\n"
    ), port)
    assert report["bind_allowed"] == 1
    assert report["denied"] == 0


def test_standard_asyncio_denies_unapproved_live_loopback_listener(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        # A real listener distinguishes policy denial from connection refusal.
        # Explicit Runner selection also resists an inherited uvloop policy.
        report = run_guard_probe(tmp_path, (
            "import asyncio\n"
            "async def probe():\n"
            "    try:\n"
            f"        await asyncio.open_connection('127.0.0.1', {port})\n"
            "    except PermissionError:\n"
            "        pass\n"
            "    else:\n"
            "        raise AssertionError('async connection bypassed guard')\n"
            "with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:\n"
            "    runner.run(probe())\n"
        ), 1)
    assert report["denied"] == 1
    assert report["connect_allowed"] == 0
