"""Guard the entrypoint's Docker socket self-heal (see git history for the
permission-denied failure it prevents)."""

from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.sh"


def test_docker_heal_runs_before_privilege_drop():
    """appuser must be added to the socket's group BEFORE runuser drops root."""
    text = ENTRYPOINT.read_text()
    assert text.index("/var/run/docker.sock") < text.index("runuser -u appuser")


def test_heal_is_nonfatal():
    """groupadd/usermod must be guarded so `set -e` can never block boot."""
    text = ENTRYPOINT.read_text()
    assert "set -e" in text
    assert "if groupadd" in text           # groupadd only under if
    assert 'usermod -aG "$DOCKER_GRP"' in text
    assert '|| \\' in text or '|| echo' in text  # usermod failure is non-fatal
