"""Guardrails for the shell tool: workspace containment, no .env, no parent/root."""

from app.tools.code_tools import CodeTools


def test_shell_blocks_escapes(tmp_path) -> None:
    tools = CodeTools(base_dir=str(tmp_path))
    blocked = [
        "cat .env",
        "cat .env.local",
        "cat ../secret",
        "cd ..",
        "cat /etc/passwd",
        "ls ~",
        "echo $HOME",
        "sudo whoami",
        "printenv",
        "dd if=/dev/zero of=/dev/sda",
        "shutdown -h now",
    ]
    for cmd in blocked:
        out = tools.shell(cmd)
        assert out.startswith("❌"), f"should block: {cmd!r} -> {out!r}"


def test_shell_allows_prose_mentioning_env(tmp_path) -> None:
    """Commit messages/docs mentioning '.env' are not file access."""
    tools = CodeTools(base_dir=str(tmp_path))
    assert "ok" in tools.shell('echo ".env is fine in prose"')


def test_shell_runs_inside_workspace(tmp_path) -> None:
    tools = CodeTools(base_dir=str(tmp_path))
    (tmp_path / "hi.txt").write_text("hello")
    assert "hello" in tools.shell("cat hi.txt")
    assert str(tmp_path) in tools.shell("pwd")
    assert tools.shell("echo ok").strip().endswith("[ok] ok")
