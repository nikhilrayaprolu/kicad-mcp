"""Generate docs/TOOLS.md from the live MCP server's tool list (names, descriptions, JSON-schema params)."""
import asyncio, json
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main():
    params = StdioServerParameters(command="bash", args=["-c", f"source {ROOT}/tools/env.sh && exec {ROOT}/.pixi/envs/default/bin/python -m llmpcb.server"], cwd=str(ROOT))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = (await s.list_tools()).tools
    out = ["# MCP tool reference", "",
           "_Generated from the running server by `clients/gen_tool_docs.py` — do not edit by hand._", ""]
    if getattr(init, "instructions", None):
        out += ["## Server instructions", "", "```", init.instructions.strip(), "```", ""]
    out += ["## Tools", "", "| Tool | Purpose |", "|---|---|"]
    for t in tools:
        first = (t.description or "").strip().splitlines()[0] if t.description else ""
        out.append(f"| [`{t.name}`](#{t.name}) | {first} |")
    out.append("")
    for t in tools:
        out += [f"### `{t.name}`", "", (t.description or "").strip(), ""]
        props = (t.input_schema or {}).get("properties", {})
        req = set((t.input_schema or {}).get("required", []))
        if props:
            out += ["| Parameter | Type | Default | Required |", "|---|---|---|---|"]
            for k, v in props.items():
                typ = v.get("type") or " / ".join(x.get("type", "?") for x in v.get("anyOf", [])) or "any"
                default = json.dumps(v["default"]) if "default" in v else ""
                out.append(f"| `{k}` | {typ} | {default} | {'yes' if k in req else ''} |")
            out.append("")
    (ROOT / "docs" / "TOOLS.md").write_text("\n".join(out) + "\n")
    print(f"{len(tools)} tools documented")

asyncio.run(main())
