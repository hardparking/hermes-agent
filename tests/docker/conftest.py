"""Shared fixtures for docker-image integration tests.

Tests in this directory build the image with the current ``Dockerfile``
and exercise it via ``docker run``. They skip when Docker is unavailable
(e.g. on developer laptops without a daemon).

Override the image with ``HERMES_TEST_IMAGE`` env var to point at a pre-built
image (faster local iteration); otherwise the ``built_image`` fixture builds
the repo's Dockerfile once per session.

"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator

import pytest

IMAGE_TAG = os.environ.get("HERMES_TEST_IMAGE", "hermes-agent-harness:latest")


def _docker_available() -> bool:
    """Return True iff a docker CLI is on PATH and the daemon answers."""
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def pytest_collection_modifyitems(config, items):  # noqa: D401 - pytest hook
    """Apply docker-suite policy: timeout bump + skip on missing docker."""
    docker_ok = _docker_available()
    skip_docker = pytest.mark.skip(
        reason="Docker not available or daemon not running",
    )
    for item in items:
        if "tests/docker/" not in str(item.fspath).replace(os.sep, "/"):
            continue
        if not docker_ok:
            item.add_marker(skip_docker)


@pytest.fixture(scope="session")
def built_image() -> str:
    """Build the image once per test session.

    Override with ``HERMES_TEST_IMAGE`` env var to point at a pre-built
    image (faster local iteration).
    """
    if os.environ.get("HERMES_TEST_IMAGE"):
        return IMAGE_TAG
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", ".."),
    )
    result = subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, repo_root],
        capture_output=True, text=True, timeout=1200,
    )
    assert result.returncode == 0, (
        f"docker build failed:\n{result.stderr[-2000:]}"
    )
    return IMAGE_TAG


@pytest.fixture
def container_name(request) -> Iterator[str]:
    """Generate a unique container name and ensure cleanup on test exit."""
    safe = request.node.name.replace("[", "_").replace("]", "_")
    name = f"hermes-test-{safe}"
    yield name
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True, timeout=10,
    )


# ---------------------------------------------------------------------------
# docker_exec — default to the unprivileged hermes user
# ---------------------------------------------------------------------------
#
# Background: every Hermes runtime path inside the container drops to UID
# 10000 (the ``hermes`` user) via ``s6-setuidgid hermes``. ``docker exec``
# without ``-u`` runs as root, which is **not** representative of how
# production code executes. PR #30136 review caught a real regression
# this way — ``Path('/proc/1/exe').resolve()`` works as root and silently
# fails (PermissionError swallowed) for hermes, so a test that ran as root
# couldn't catch a feature that was inert for the actual runtime user.
#
# Tests in this directory MUST exercise the realistic user context. The
# helpers below run every probe under ``-u hermes`` unless a specific
# test explicitly opts into ``user="root"`` (rare — e.g. inspecting
# /proc/1/exe itself, chowning a volume).
# ---------------------------------------------------------------------------


def docker_exec(
    container: str,
    *args: str,
    user: str = "hermes",
    timeout: int = 30,
    extra_docker_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run a command inside ``container`` as ``user`` (default: hermes).

    Returns the CompletedProcess with text=True, capture_output=True.

    Pass ``user="root"`` only when the test specifically needs root
    capabilities (e.g. reading /proc/1/exe, manipulating ownership).
    Most tests should use the default.
    """
    cmd = ["docker", "exec", "-u", user, *extra_docker_args, container, *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )


def docker_exec_sh(
    container: str,
    command: str,
    *,
    user: str = "hermes",
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run ``sh -c <command>`` inside the container as ``user``."""
    return docker_exec(
        container, "sh", "-c", command, user=user, timeout=timeout,
    )


def wait_for_container_ready(
    container: str,
    *,
    deadline_s: float = 30.0,
    interval_s: float = 0.25,
) -> None:
    """Poll until the container has finished s6 cont-init (stage2 + reconcile).

    The readiness signal is ``profile=default`` appearing in
    ``/opt/data/logs/container-boot.log``, which the 02-reconcile-profiles
    cont-init script writes on every boot. That log entry fires AFTER
    stage2-hook.sh completes, so by the time it appears the full
    cont-init chain (UID remap, chown, config seeding, skills sync,
    browser discovery, config migration) has run.

    Raises ``TimeoutError`` if the container never becomes ready — much
    better than a fixed ``time.sleep()`` that either wastes time on fast
    machines or flakes on slow ones.
    """
    import time as _time

    end = _time.monotonic() + deadline_s
    while _time.monotonic() < end:
        r = docker_exec(
            container,
            "sh", "-c",
            "cat /opt/data/logs/container-boot.log 2>/dev/null",
            timeout=5,
        )
        if r.returncode == 0 and "profile=default" in r.stdout:
            return
        _time.sleep(interval_s)
    raise TimeoutError(
        f"container {container} did not finish cont-init within {deadline_s}s"
    )
