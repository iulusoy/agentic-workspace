"""Interactive chat with Claude using BioCypher MCP as the tool backend.

Uses the Anthropic Messages API native remote MCP connector — Claude calls the
MCP tools server-side; no local stdio bridge required.

Env vars:
- ANTHROPIC_API_KEY (required)
- BIOCYPHER_MCP_URL (default: https://mcp.biocypher.org/mcp)
- BIOCYPHER_MCP_AUTH_HEADER (optional, e.g. "Bearer <token>")
- CLAUDE_MODEL (default: claude-opus-4-7)
"""

import os
import sys

import anthropic

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-7")
MCP_URL = os.getenv("BIOCYPHER_MCP_URL", "https://mcp.biocypher.org/mcp")
SYSTEM_PROMPT = (
    "You are a helpful assistant with access to BioCypher tools via MCP. "
    "Use them when relevant to answer questions about biomedical knowledge "
    "graphs, available workflows, schema, and data sources."
)
MCP_BETA = "mcp-client-2025-04-04"


def build_mcp_server() -> dict:
    server: dict = {
        "type": "url",
        "url": MCP_URL,
        "name": "biocypher-mcp",
    }
    auth = os.getenv("BIOCYPHER_MCP_AUTH_HEADER")
    if auth:
        token = auth.removeprefix("Bearer ").strip()
        server["authorization_token"] = token
    return server


def render_response(content) -> str:
    text_parts: list[str] = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(block.text)
        elif btype == "mcp_tool_use":
            name = getattr(block, "name", "?")
            server = getattr(block, "server_name", "?")
            print(f"\n[mcp call] {server}.{name}", file=sys.stderr, flush=True)
        elif btype == "mcp_tool_result":
            is_error = getattr(block, "is_error", False)
            status = "error" if is_error else "ok"
            print(f"[mcp result {status}]", file=sys.stderr, flush=True)
    return "\n".join(p for p in text_parts if p)


def main() -> None:
    client = anthropic.Anthropic()
    mcp_servers = [build_mcp_server()]
    history: list[dict] = []

    print(f"Chat with Claude ({MODEL}) + BioCypher MCP ({MCP_URL}).")
    print("Type a message and press Enter. Ctrl-D or empty line to quit.\n")

    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            break

        history.append({"role": "user", "content": user})

        try:
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=8192,
                system=SYSTEM_PROMPT,
                messages=history,
                mcp_servers=mcp_servers,
                betas=[MCP_BETA],
            )
        except anthropic.APIStatusError as e:
            print(f"\n[api error {e.status_code}] {e.message}", file=sys.stderr)
            history.pop()
            continue

        history.append({"role": "assistant", "content": response.content})
        text = render_response(response.content)
        print(f"\nclaude> {text}\n")


if __name__ == "__main__":
    main()
