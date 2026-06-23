"""Runtime smoke tests for Docker gateway_state.json bootstrap seeding.

Build the real image and verify the actual runtime behavior:

  1. HERMES_GATEWAY_BOOTSTRAP_STATE=running on a fresh volume seeds
     gateway_state.json with running state
  2. An existing gateway_state.json is never clobbered (first-boot-only)
  3. No env var = no seed (default down-on-first-boot preserved)
  4. Only literal "running" is honored; other values are ignored
"""
from __future__ import annotations

import json
import subprocess

from tests.docker.conftest import docker_exec, docker_exec_sh, wait_for_container_ready


def _start_container(
    built_image: str, name: str, *env: str,
) -> str:
    """Start a container with given env vars, return its name."""
    args = ["docker", "run", "-d", "--name", name]
    for e in env:
        args.extend(["-e", e])
    args.extend([built_image, "sleep", "infinity"])
    subprocess.run(args, check=True, capture_output=True, timeout=60)
    wait_for_container_ready(name)
    return name


def test_seeds_running_state_on_blank_volume(
    built_image: str, container_name: str,
) -> None:
    """HERMES_GATEWAY_BOOTSTRAP_STATE=running on a fresh volume must
    seed gateway_state.json with a valid running state."""
    _start_container(
        built_image, container_name,
        "HERMES_GATEWAY_BOOTSTRAP_STATE=running",
    )

    r = docker_exec_sh(
        container_name,
        "cat /opt/data/gateway_state.json 2>/dev/null || echo NONE",
        timeout=10,
    )
    assert r.stdout.strip() != "NONE", (
        f"gateway_state.json not seeded on fresh volume: {r.stdout}"
    )
    state = json.loads(r.stdout.strip())
    assert state.get("gateway_state") == "running", (
        f"expected gateway_state=running, got: {state}"
    )


def test_does_not_clobber_existing_state(
    built_image: str, container_name: str,
) -> None:
    """An existing gateway_state.json must never be overwritten by the
    seed, even when the bootstrap env var says running.

    We use a named volume so we can pre-create the state file before
    the container boots. The [ ! -f ] guard in stage2 must skip seeding
    because the file already exists. We check the file immediately after
    boot — before the gateway service has a chance to write its own
    state — by reading it as fast as possible after container start.
    """
    import json as _json

    volume = f"{container_name}-vol"
    subprocess.run(
        ["docker", "volume", "create", volume],
        check=True, capture_output=True, timeout=10,
    )

    # Pre-create the state file via a throwaway container
    existing = _json.dumps({"gateway_state": "stopped", "pid": 123})
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{volume}:/opt/data",
         "--entrypoint", "sh", built_image,
         "-c", f"printf '{existing}\\n' > /opt/data/gateway_state.json"],
        check=True, capture_output=True, timeout=30,
    )

    # Boot with the env var set — stage2 must NOT clobber the existing file
    subprocess.run(
        ["docker", "run", "-d", "--name", container_name,
         "-v", f"{volume}:/opt/data",
         "-e", "HERMES_GATEWAY_BOOTSTRAP_STATE=running",
         built_image, "sleep", "infinity"],
        check=True, capture_output=True, timeout=60,
    )
    # Read the file as quickly as possible — the gateway service may
    # start and write its own state, but the stage2 [ ! -f ] guard runs
    # during cont-init (before any service starts), so the file must
    # still be our "stopped" state at this point.
    wait_for_container_ready(container_name)
    r = docker_exec_sh(
        container_name, "cat /opt/data/gateway_state.json", timeout=10,
    )
    state = _json.loads(r.stdout.strip())
    assert state.get("gateway_state") == "stopped", (
        f"existing state was clobbered by bootstrap seed: {state}"
    )

    # Cleanup
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True, timeout=10,
    )
    subprocess.run(
        ["docker", "volume", "rm", "-f", volume],
        capture_output=True, timeout=10,
    )


def test_no_seed_when_env_unset(
    built_image: str, container_name: str,
) -> None:
    """No HERMES_GATEWAY_BOOTSTRAP_STATE = no seed file written."""
    _start_container(built_image, container_name)

    r = docker_exec_sh(
        container_name,
        "test -f /opt/data/gateway_state.json && "
        "echo EXISTS || echo ABSENT",
        timeout=10,
    )
    assert "ABSENT" in r.stdout, (
        f"gateway_state.json was seeded without the env var: {r.stdout}"
    )


def test_non_running_value_ignored(
    built_image: str, container_name: str,
) -> None:
    """Only literal 'running' is honored; any other value is ignored."""
    for bogus in ("stopped", "Running", "1", "true", "starting"):
        # Need a fresh container per iteration
        name = f"{container_name}-{bogus}"
        _start_container(
            built_image, name,
            f"HERMES_GATEWAY_BOOTSTRAP_STATE={bogus}",
        )
        r = docker_exec_sh(
            name,
            "test -f /opt/data/gateway_state.json && "
            "echo EXISTS || echo ABSENT",
            timeout=10,
        )
        assert "ABSENT" in r.stdout, (
            f"bogus value {bogus!r} should not seed a state file: {r.stdout}"
        )
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, timeout=10,
        )