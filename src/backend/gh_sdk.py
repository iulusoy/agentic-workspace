import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

from copilot import CopilotClient, SubprocessConfig
from copilot.session import PermissionHandler


def build_biocypher_mcp_config() -> dict:
    """
    Build MCP config for the BioCypher MCP server.

    Configure through environment variables:
    - BIOCYPHER_MCP_URL (default: https://mcp.biocypher.org/mcp)
    - BIOCYPHER_MCP_AUTH_HEADER (optional, e.g. "Bearer <token>")
    - BIOCYPHER_MCP_BRIDGE_COMMAND (default: npx)
    - BIOCYPHER_MCP_BRIDGE_ARGS (optional extra args appended to bridge)
    - BIOCYPHER_MCP_TOOLS (default: "*", comma-separated tool names)
    """
    url = os.getenv("BIOCYPHER_MCP_URL", "https://mcp.biocypher.org/mcp")
    bridge_command = os.getenv("BIOCYPHER_MCP_BRIDGE_COMMAND", "npx")
    bridge_args = ["-y", "mcp-remote", url, "--transport", "http-only"]

    auth_header = os.getenv("BIOCYPHER_MCP_AUTH_HEADER")
    if auth_header:
        bridge_args.extend(["--header", f"Authorization: {auth_header}"])

    extra_bridge_args = os.getenv("BIOCYPHER_MCP_BRIDGE_ARGS", "")
    if extra_bridge_args:
        bridge_args.extend(shlex.split(extra_bridge_args))

    tools_env = os.getenv("BIOCYPHER_MCP_TOOLS", "*").strip()
    if tools_env == "*":
        allowed_tools = ["*"]
    else:
        allowed_tools = [t.strip() for t in tools_env.split(",") if t.strip()]
        if not allowed_tools:
            allowed_tools = ["*"]

    server_config = {
        "type": "stdio",
        "command": bridge_command,
        "args": bridge_args,
        "tools": allowed_tools,
        "timeout": 60_000,
    }

    return {"biocypher-mcp": server_config}


def get_configured_mcp_methods(mcp_servers: dict) -> list[str]:
    methods: list[str] = []
    for _, server_config in mcp_servers.items():
        methods.extend(server_config.get("tools", []))
    return methods


def load_biocypher_from_cursor_mcp() -> dict:
    """
    Load the BioCypher URL from Cursor MCP config and build bridge config.
    """
    cursor_mcp_path = Path.home() / ".cursor" / "mcp.json"
    if not cursor_mcp_path.exists():
        return build_biocypher_mcp_config()

    with cursor_mcp_path.open("r", encoding="utf-8") as f:
        cursor_cfg = json.load(f)

    servers = cursor_cfg.get("mcpServers", {})
    biocypher_cfg = servers.get("biocypher-mcp")
    if not biocypher_cfg:
        return build_biocypher_mcp_config()

    if biocypher_cfg.get("url"):
        os.environ.setdefault("BIOCYPHER_MCP_URL", biocypher_cfg["url"])
    return build_biocypher_mcp_config()


async def main() -> None:
    mcp_servers = load_biocypher_from_cursor_mcp()
    server_cfg = mcp_servers["biocypher-mcp"]
    configured_methods = get_configured_mcp_methods(mcp_servers)
    print("Chat with Copilot SDK + BioCypher MCP.")
    print("Type a message and press Enter. Ctrl-D or empty line to quit.\n")
    print("Configured MCP methods:", ", ".join(configured_methods) if configured_methods else "(none)")
    print("MCP server type:", server_cfg["type"])
    print("MCP server command:", server_cfg["command"])
    print("MCP server args:", server_cfg["args"])

    # These CLI flags are often required to let SDK sessions access tool calls.
    client = CopilotClient(
        SubprocessConfig(
            cli_args=["--allow-all-tools", "--allow-all-paths"],
            log_level="debug",
        )
    )
    await client.start()

    turn_state = {"saw_assistant_delta": False}

    def handle_event(event):
        event_type = str(getattr(event.type, "value", event.type)).lower()

        if event_type in {"info", "warning", "error"}:
            message = getattr(event.data, "message", "")
            if message:
                print(f"\n[{event_type.lower()}] {message}", file=sys.stderr, flush=True)

        if "assistant.message_delta" in event_type:
            delta = getattr(event.data, "delta_content", "")
            turn_state["saw_assistant_delta"] = True
            sys.stdout.write(delta)
            sys.stdout.flush()

        if "tool.execution_start" in event_type:
            tool_name = getattr(event.data, "tool_name", "(unknown)")
            mcp_server_name = getattr(event.data, "mcp_server_name", None)
            mcp_tool_name = getattr(event.data, "mcp_tool_name", None)
            if mcp_server_name or mcp_tool_name:
                print(
                    f"\n[tool-start] {tool_name} "
                    f"(mcp_server={mcp_server_name}, mcp_tool={mcp_tool_name})",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(f"\n[tool-start] {tool_name}", file=sys.stderr, flush=True)

        if "tool.execution_complete" in event_type or "tool.execution_error" in event_type:
            tool_name = getattr(event.data, "tool_name", "(unknown)")
            print(f"\n[{event_type.lower()}] {tool_name}", file=sys.stderr, flush=True)

    session = await client.create_session(
        on_permission_request=PermissionHandler.approve_all,
        on_event=handle_event,
        model="gpt-4.1",
        streaming=True,
        mcp_servers=mcp_servers,
    )
    session.on(handle_event)

    try:
        while True:
            try:
                user = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user:
                break

            turn_state["saw_assistant_delta"] = False
            print("copilot> ", end="", flush=True)
            response = await session.send_and_wait(user)
            if not turn_state["saw_assistant_delta"]:
                content = getattr(response.data, "content", "")
                print(content if content else "(no content)", end="")
            print()
    finally:
        await session.disconnect()
        await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
