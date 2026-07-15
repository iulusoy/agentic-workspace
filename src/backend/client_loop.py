"""Client-side tool loop: chat with an LLM + BioCypher MCP, MCP executed locally.

This process is the MCP client. It connects directly to the BioCypher MCP
server (streamable HTTP), executes every tool call itself, and only the
(optionally truncated) tool results enter the model's context. The Anthropic
remote-MCP connector is not used — Anthropic never talks to the MCP server.

The LLM provider is swappable via base URL:
- Anthropic (default): set ANTHROPIC_API_KEY
- Local model: run an Anthropic-compatible endpoint (LiteLLM proxy,
  llama.cpp server) and set ANTHROPIC_BASE_URL + a dummy ANTHROPIC_API_KEY

Env vars:
- ANTHROPIC_API_KEY          (required; any non-empty value for local models)
- ANTHROPIC_BASE_URL         (optional, e.g. http://localhost:4000 for LiteLLM)
- CLAUDE_MODEL               (default: claude-opus-4-8)
- BIOCYPHER_MCP_URL          (default: https://mcp.biocypher.org/mcp)
- BIOCYPHER_MCP_AUTH_HEADER  (optional, e.g. "Bearer <token>")
- MCP_RESULT_MAX_CHARS       (default: 20000 — cap on tool-result chars that
                              reach model context; rest stays local)
- FILE_TOOLS_ROOT            (default: cwd — root dir the read/write/edit
                              file tools are confined to)

Run:
    python src/backend/client_loop.py               # interactive chat
    python src/backend/client_loop.py --list-tools  # MCP connectivity check
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import anthropic
from anthropic import AsyncAnthropic
from anthropic.lib.tools import beta_async_tool
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-8")
MCP_URL = os.getenv("BIOCYPHER_MCP_URL", "https://mcp.biocypher.org/mcp")
RESULT_MAX_CHARS = int(os.getenv("MCP_RESULT_MAX_CHARS", "20000"))
FILE_ROOT = Path(os.getenv("FILE_TOOLS_ROOT", ".")).resolve()
SYSTEM_PROMPT = (
    "You are a helpful assistant with access to BioCypher tools via MCP. "
    "Use them when relevant to answer questions about biomedical knowledge "
    "graphs, available workflows, schema, and data sources. "
    "You also have list_dir, read_file, write_file, and edit_file tools for local "
    f"files under the workspace root ({FILE_ROOT}), and a run_command tool that "
    "executes shell commands there in the user-selected Python environment. "
    "MANDATORY when creating a new BioCypher project or adapter: first call "
    "check_project_exists and get_cookiecutter_instructions, then scaffold the "
    "project by running cookiecutter via run_command exactly as those "
    "instructions say (pip-install cookiecutter into the environment first if "
    "missing). Never create the project or adapter directory structure by hand "
    "with write_file; only fill in or edit files inside the scaffolded structure. "
    "MANDATORY after implementing or changing code: run the test suite via "
    "run_command (pytest) and fix failures until it passes, or report exactly "
    "what still fails and why. "
    f"Tool results are truncated after {RESULT_MAX_CHARS} characters; if a "
    "result is cut off, narrow the tool arguments (filters, specific phases "
    "or topics) instead of repeating the same call."
)


def thinking_config() -> dict | None:
    """Adaptive thinking against Anthropic; off for local endpoints.

    Local Anthropic-compatible servers (LiteLLM, llama.cpp) may reject the
    thinking parameter, so it defaults on only when ANTHROPIC_BASE_URL is
    unset. Override with CLAUDE_THINKING=adaptive|off.
    """
    mode = os.getenv("CLAUDE_THINKING", "").lower()
    if mode == "off":
        return None
    if mode == "adaptive" or not os.getenv("ANTHROPIC_BASE_URL"):
        return {"type": "adaptive"}
    return None


def mcp_headers() -> dict[str, str]:
    auth = os.getenv("BIOCYPHER_MCP_AUTH_HEADER")
    return {"Authorization": auth} if auth else {}


def render_tool_result(result) -> str:
    """Flatten an MCP CallToolResult to text for model context."""
    if result.structuredContent:
        text = json.dumps(result.structuredContent)
    else:
        parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        text = "\n".join(parts)
    if result.isError:
        text = f"[tool error] {text}"
    return text


def make_tool(mcp_tool_def, session: ClientSession):
    """Wrap one MCP tool as a runnable tool with a result-size guard.

    The full result stays in this process; only the first RESULT_MAX_CHARS
    are handed to the model.
    """
    tool_name = mcp_tool_def.name

    async def call(**kwargs):
        print(f"\n[tool] {tool_name} {json.dumps(kwargs)}", file=sys.stderr, flush=True)
        result = await session.call_tool(name=tool_name, arguments=kwargs)
        text = render_tool_result(result)
        if len(text) > RESULT_MAX_CHARS:
            omitted = len(text) - RESULT_MAX_CHARS
            text = (
                text[:RESULT_MAX_CHARS]
                + f"\n[truncated: {omitted} chars omitted before model context]"
            )
        print(f"[tool done] {tool_name} ({len(text)} chars to context)", file=sys.stderr, flush=True)
        return text

    return beta_async_tool(
        call,
        name=tool_name,
        description=mcp_tool_def.description or "",
        input_schema=mcp_tool_def.inputSchema,
    )


def _resolve_path(path: str) -> Path:
    """Resolve a tool-supplied path and confine it to FILE_ROOT."""
    resolved = (FILE_ROOT / path).resolve()
    if not resolved.is_relative_to(FILE_ROOT):
        raise ValueError(f"path escapes workspace root {FILE_ROOT}: {path}")
    return resolved


@beta_async_tool
async def list_dir(path: str = ".") -> str:
    """List the files and subdirectories of a directory.

    Args:
        path: Directory path, relative to the workspace root. Defaults to
            the workspace root itself.
    """
    print(f"\n[tool] list_dir {path}", file=sys.stderr, flush=True)
    try:
        entries = sorted(
            _resolve_path(path).iterdir(),
            key=lambda p: (not p.is_dir(), p.name),
        )
    except (OSError, ValueError) as e:
        return f"[tool error] {e}"
    if not entries:
        return "[empty directory]"
    lines = [f"{e.name}/" if e.is_dir() else e.name for e in entries]
    text = "\n".join(lines)
    if len(text) > RESULT_MAX_CHARS:
        omitted = len(text) - RESULT_MAX_CHARS
        text = text[:RESULT_MAX_CHARS] + f"\n[truncated: {omitted} chars omitted]"
    return text


@beta_async_tool
async def read_file(path: str) -> str:
    """Read a text file and return its content.

    Args:
        path: File path, relative to the workspace root.
    """
    print(f"\n[tool] read_file {path}", file=sys.stderr, flush=True)
    try:
        text = _resolve_path(path).read_text()
    except (OSError, ValueError) as e:
        return f"[tool error] {e}"
    if len(text) > RESULT_MAX_CHARS:
        omitted = len(text) - RESULT_MAX_CHARS
        text = text[:RESULT_MAX_CHARS] + f"\n[truncated: {omitted} chars omitted]"
    return text


@beta_async_tool
async def write_file(path: str, content: str) -> str:
    """Create or overwrite a text file with the given content.

    Args:
        path: File path, relative to the workspace root. Parent directories
            are created as needed.
        content: Full content to write.
    """
    print(f"\n[tool] write_file {path} ({len(content)} chars)", file=sys.stderr, flush=True)
    try:
        target = _resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    except (OSError, ValueError) as e:
        return f"[tool error] {e}"
    return f"wrote {len(content)} chars to {path}"


@beta_async_tool
async def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace an exact string in an existing text file.

    Args:
        path: File path, relative to the workspace root.
        old_string: Exact text to replace; must occur exactly once. Include
            surrounding lines to make it unique.
        new_string: Replacement text.
    """
    print(f"\n[tool] edit_file {path}", file=sys.stderr, flush=True)
    try:
        target = _resolve_path(path)
        text = target.read_text()
    except (OSError, ValueError) as e:
        return f"[tool error] {e}"
    count = text.count(old_string)
    if count == 0:
        return "[tool error] old_string not found in file"
    if count > 1:
        return f"[tool error] old_string occurs {count} times; add context to make it unique"
    try:
        target.write_text(text.replace(old_string, new_string, 1))
    except OSError as e:
        return f"[tool error] {e}"
    return f"edited {path}"


# Bin directory of the Python environment used by run_command; set at chat
# startup (user choice, defaults to the environment running this script).
EXEC_BIN: Path | None = None


def _exec_env() -> dict[str, str]:
    env = os.environ.copy()
    if EXEC_BIN is not None:
        env["PATH"] = f"{EXEC_BIN}{os.pathsep}{env.get('PATH', '')}"
    return env


@beta_async_tool
async def run_command(command: str, timeout_seconds: int = 300) -> str:
    """Run a shell command in the workspace root.

    The selected Python environment's bin directory is first on PATH, so
    `python`, `pip`, `pytest`, and `cookiecutter` resolve from it. Use this to
    scaffold projects with cookiecutter and to run code and tests.

    Args:
        command: Shell command to run (cwd is the workspace root).
        timeout_seconds: Kill the command after this many seconds (default 300).
    """
    print(f"\n[tool] run_command {command}", file=sys.stderr, flush=True)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=FILE_ROOT,
            env=_exec_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"[tool error] command timed out after {timeout_seconds}s"
    except OSError as e:
        return f"[tool error] {e}"
    text = out.decode(errors="replace")
    if len(text) > RESULT_MAX_CHARS:
        omitted = len(text) - RESULT_MAX_CHARS
        text = text[:RESULT_MAX_CHARS] + f"\n[truncated: {omitted} chars omitted]"
    print(f"[tool done] run_command (exit {proc.returncode})", file=sys.stderr, flush=True)
    return f"[exit {proc.returncode}]\n{text}"


FILE_TOOLS = [list_dir, read_file, write_file, edit_file, run_command]


async def print_stream(stream) -> None:
    """Print one assistant message as it streams: text deltas live, thinking
    and tool-call markers to stderr."""
    printed_text = False
    thinking_marked = False
    async for event in stream:
        if event.type == "text":
            if not printed_text:
                print("\nassistant> ", end="", flush=True)
                printed_text = True
            print(event.text, end="", flush=True)
        elif event.type == "thinking":
            if not thinking_marked:
                print("[thinking...]", file=sys.stderr, flush=True)
                thinking_marked = True
        elif event.type == "content_block_start" and event.content_block.type == "tool_use":
            print(f"\n[tool call requested] {event.content_block.name}", file=sys.stderr, flush=True)
    if printed_text:
        print(flush=True)


def _resolve_env_bin(raw: str) -> Path:
    """Turn user input (python binary, env root, or bin dir) into a bin dir."""
    p = Path(raw).expanduser().resolve()
    if p.is_file():
        return p.parent
    if (p / "bin").is_dir():
        return p / "bin"
    return p


async def select_exec_env(prompt: PromptSession) -> None:
    """Ask which Python environment run_command should use (Enter = current)."""
    global EXEC_BIN
    default_bin = Path(sys.executable).parent
    with patch_stdout():
        raw = (
            await prompt.prompt_async(
                f"python env for run_command (path to env, bin dir, or python) "
                f"[Enter = {default_bin}]: "
            )
        ).strip()
    EXEC_BIN = _resolve_env_bin(raw) if raw else default_bin
    python = EXEC_BIN / "python"
    marker = "" if python.exists() else " (warning: no python found there)"
    print(f"run_command environment: {EXEC_BIN}{marker}")


async def chat(session: ClientSession, tools) -> None:
    client = AsyncAnthropic()  # honors ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL
    history: list[dict] = []
    thinking = thinking_config()
    # prompt_toolkit: async input keeps the event loop (and the MCP HTTP
    # session) alive while waiting, and bracketed paste keeps pasted
    # newlines in the edit buffer instead of submitting on them.
    prompt: PromptSession = PromptSession()
    await select_exec_env(prompt)

    print(f"Chat with {MODEL} + BioCypher MCP ({MCP_URL}), client-side tool loop.")
    print(f"Tools: {', '.join(t.name for t in tools)}")
    print("Type a message and press Enter. 'exit', 'quit', Ctrl-D, or empty line to quit.\n")

    while True:
        try:
            with patch_stdout():
                user = (await prompt.prompt_async("you> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user or user.lower() in ("exit", "quit"):
            break

        snapshot = len(history)
        history.append({"role": "user", "content": user})

        runner = client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=history,
            stream=True,
            # Auto-cache the prefix up to the latest turn; each turn then
            # reads the previous turns from cache instead of re-billing them.
            cache_control={"type": "ephemeral"},
            **({"thinking": thinking} if thinking else {}),
        )
        # Mirror the runner's conversation into our history so the next turn
        # continues from the full context (runner keeps its own copy).
        try:
            async for stream in runner:
                async with stream:
                    await print_stream(stream)
                    message = await stream.get_final_message()
                u = message.usage
                print(
                    f"[usage] in={u.input_tokens} cache_read={u.cache_read_input_tokens} "
                    f"cache_write={u.cache_creation_input_tokens} out={u.output_tokens}",
                    file=sys.stderr,
                    flush=True,
                )
                history.append({"role": "assistant", "content": message.content})
                tool_response = await runner.generate_tool_call_response()
                if tool_response is not None:
                    history.append(tool_response)
        except anthropic.APIError as e:
            # Discard the whole partial turn: a mid-loop failure can leave an
            # assistant tool_use without its tool_result, which would 400 on
            # every following request.
            del history[snapshot:]
            print(f"\n[llm error] {e}\n", file=sys.stderr, flush=True)


async def main() -> None:
    if "--list-tools" not in sys.argv and not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY is not set. Set it to your Anthropic key, or to any "
            "non-empty value together with ANTHROPIC_BASE_URL for a local model."
        )
    async with streamablehttp_client(MCP_URL, headers=mcp_headers()) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tools = [make_tool(t, session) for t in tools_result.tools] + FILE_TOOLS

            if "--list-tools" in sys.argv:
                for t in tools_result.tools:
                    print(f"{t.name}: {(t.description or '').strip().splitlines()[0] if t.description else ''}")
                return

            await chat(session, tools)


if __name__ == "__main__":
    asyncio.run(main())
