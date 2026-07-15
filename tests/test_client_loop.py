"""Unit tests for src/backend/client_loop.py — no network, no API key."""

import json
from types import SimpleNamespace

import pytest

from conftest import run


# ---------------------------------------------------------------- config


def test_thinking_config_default_anthropic(monkeypatch):
    import client_loop as cl

    monkeypatch.delenv("CLAUDE_THINKING", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert cl.thinking_config() == {"type": "adaptive"}


def test_thinking_config_off_for_local_endpoint(monkeypatch):
    import client_loop as cl

    monkeypatch.delenv("CLAUDE_THINKING", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:4000")
    assert cl.thinking_config() is None


def test_thinking_config_explicit_override(monkeypatch):
    import client_loop as cl

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:4000")
    monkeypatch.setenv("CLAUDE_THINKING", "adaptive")
    assert cl.thinking_config() == {"type": "adaptive"}
    monkeypatch.setenv("CLAUDE_THINKING", "off")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert cl.thinking_config() is None


def test_mcp_headers(monkeypatch, cl):
    monkeypatch.delenv("BIOCYPHER_MCP_AUTH_HEADER", raising=False)
    assert cl.mcp_headers() == {}
    monkeypatch.setenv("BIOCYPHER_MCP_AUTH_HEADER", "Bearer abc")
    assert cl.mcp_headers() == {"Authorization": "Bearer abc"}


# ------------------------------------------------------- path confinement


def test_resolve_path_inside_root(workspace, cl):
    (workspace / "sub").mkdir()
    assert cl._resolve_path("sub") == workspace / "sub"


@pytest.mark.parametrize("path", ["..", "../outside.txt", "a/../../b"])
def test_resolve_path_rejects_escape(workspace, cl, path):
    with pytest.raises(ValueError, match="escapes workspace root"):
        cl._resolve_path(path)


def test_resolve_path_rejects_symlink_escape(workspace, tmp_path_factory, cl):
    outside = tmp_path_factory.mktemp("outside")
    (workspace / "link").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes workspace root"):
        cl._resolve_path("link/x.txt")


# ------------------------------------------------------------ file tools


def test_list_dir_sorts_dirs_first(workspace, cl):
    (workspace / "zdir").mkdir()
    (workspace / "afile.txt").touch()
    out = run(cl.list_dir.call({}))
    assert out == "zdir/\nafile.txt"


def test_list_dir_empty_and_missing(workspace, cl):
    assert run(cl.list_dir.call({})) == "[empty directory]"
    assert run(cl.list_dir.call({"path": "nope"})).startswith("[tool error]")


def test_read_write_roundtrip(workspace, cl):
    msg = run(cl.write_file.call({"path": "a/b/c.txt", "content": "hello"}))
    assert msg == "wrote 5 chars to a/b/c.txt"
    assert (workspace / "a/b/c.txt").read_text() == "hello"
    assert run(cl.read_file.call({"path": "a/b/c.txt"})) == "hello"


def test_read_file_missing_and_escape(workspace, cl):
    assert run(cl.read_file.call({"path": "nope.txt"})).startswith("[tool error]")
    assert "escapes workspace root" in run(cl.read_file.call({"path": "../x"}))


def test_read_file_truncates(workspace, small_cap, cl):
    (workspace / "big.txt").write_text("x" * 120)
    out = run(cl.read_file.call({"path": "big.txt"}))
    assert out.startswith("x" * small_cap)
    assert "[truncated: 70 chars omitted]" in out


def test_edit_file(workspace, cl):
    (workspace / "f.txt").write_text("one two one")
    assert "occurs 2 times" in run(
        cl.edit_file.call({"path": "f.txt", "old_string": "one", "new_string": "1"})
    )
    assert "not found" in run(
        cl.edit_file.call({"path": "f.txt", "old_string": "zzz", "new_string": "1"})
    )
    assert (
        run(
            cl.edit_file.call({"path": "f.txt", "old_string": "two", "new_string": "2"})
        )
        == "edited f.txt"
    )
    assert (workspace / "f.txt").read_text() == "one 2 one"


# ---------------------------------------------------------- MCP wrapping


def _mcp_result(text=None, structured=None, is_error=False):
    content = [SimpleNamespace(type="text", text=t) for t in (text or [])]
    return SimpleNamespace(
        structuredContent=structured, content=content, isError=is_error
    )


def test_render_tool_result_text_and_structured(cl):
    assert cl.render_tool_result(_mcp_result(text=["a", "b"])) == "a\nb"
    assert cl.render_tool_result(_mcp_result(structured={"k": 1})) == json.dumps(
        {"k": 1}
    )
    assert (
        cl.render_tool_result(_mcp_result(text=["boom"], is_error=True))
        == "[tool error] boom"
    )


class FakeSession:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def _tool_def(name="my_tool"):
    return SimpleNamespace(
        name=name,
        description="desc",
        inputSchema={
            "type": "object",
            "properties": {"q": {"type": "string"}},
        },
    )


def test_make_tool_passes_args_and_returns_text(cl):
    session = FakeSession(_mcp_result(text=["result text"]))
    tool = cl.make_tool(_tool_def(), session)
    assert tool.name == "my_tool"
    out = run(tool.call({"q": "hello"}))
    assert out == "result text"
    assert session.calls == [("my_tool", {"q": "hello"})]


def test_make_tool_truncates_long_result(small_cap, cl):
    session = FakeSession(_mcp_result(text=["y" * 200]))
    tool = cl.make_tool(_tool_def(), session)
    out = run(tool.call({"q": "x"}))
    assert out.startswith("y" * small_cap)
    assert "[truncated: 150 chars omitted before model context]" in out


# ------------------------------------------------------------ run_command


def test_exec_env_prepends_bin(monkeypatch, tmp_path, cl):
    monkeypatch.setattr(cl, "EXEC_BIN", tmp_path)
    env = cl._exec_env()
    assert env["PATH"].startswith(str(tmp_path))
    monkeypatch.setattr(cl, "EXEC_BIN", None)
    assert cl._exec_env()["PATH"] == cl.os.environ["PATH"]


def test_resolve_env_bin(tmp_path, cl):
    binary = tmp_path / "python"
    binary.touch()
    assert cl._resolve_env_bin(str(binary)) == tmp_path
    env_root = tmp_path / "env"
    (env_root / "bin").mkdir(parents=True)
    assert cl._resolve_env_bin(str(env_root)) == env_root / "bin"
    plain = tmp_path / "plainbin"
    plain.mkdir()
    assert cl._resolve_env_bin(str(plain)) == plain


def test_run_command_success_and_exit_code(workspace, cl):
    out = run(cl.run_command.call({"command": "echo hi"}))
    assert out == "[exit 0]\nhi\n"
    out = run(cl.run_command.call({"command": "exit 3"}))
    assert out.startswith("[exit 3]")


def test_run_command_uses_exec_bin_path(workspace, tmp_path_factory, monkeypatch, cl):
    fake_bin = tmp_path_factory.mktemp("bin")
    marker = fake_bin / "markertool"
    marker.write_text("#!/bin/sh\necho from-fake-env\n")
    marker.chmod(0o755)
    monkeypatch.setattr(cl, "EXEC_BIN", fake_bin)
    out = run(cl.run_command.call({"command": "markertool"}))
    assert out == "[exit 0]\nfrom-fake-env\n"


def test_run_command_runs_in_workspace_root(workspace, cl):
    out = run(cl.run_command.call({"command": "pwd"}))
    assert out.strip().endswith(str(workspace))


def test_run_command_timeout(workspace, cl):
    out = run(cl.run_command.call({"command": "sleep 5", "timeout_seconds": 1}))
    assert out == "[tool error] command timed out after 1s"


def test_run_command_merges_stderr(workspace, cl):
    out = run(cl.run_command.call({"command": "echo err >&2"}))
    assert out == "[exit 0]\nerr\n"


def test_run_command_truncates(workspace, small_cap, cl):
    out = run(cl.run_command.call({"command": "printf 'z%.0s' $(seq 1 200)"}))
    assert "[truncated:" in out
    assert out.startswith("[exit 0]\n" + "z" * small_cap)


# ------------------------------------------------------- system prompt


def test_system_prompt_mandates_cookiecutter_and_pytest(cl):
    for needle in (
        "get_cookiecutter_instructions",
        "check_project_exists",
        "run_command",
        "pytest",
    ):
        assert needle in cl.SYSTEM_PROMPT
