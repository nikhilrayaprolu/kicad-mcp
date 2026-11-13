"""Drive the llm-pcb MCP server over real stdio MCP.

usage: pixi run python clients/mcp_call.py <project_dir> <calls.json | ->
calls.json = [{"tool": name, "args": {...}}, ...]   (project_open is issued first automatically)
Prints each result (truncated) and saves returned images; logs to <project>/client_calls.jsonl.
"""
import asyncio, base64, json, sys, time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main(project: str, calls: list, maxlen: int):
    params = StdioServerParameters(command="bash", args=["-c", f"source {ROOT}/tools/env.sh && exec {ROOT}/.pixi/envs/default/bin/python -m llmpcb.server"],
                                   cwd=str(ROOT))
    log = Path(project) / "client_calls.jsonl"
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            first = {"tool": "project_open", "args": {"path": project}}
            if calls and calls[0]["tool"] == "project_open":
                first = calls.pop(0)
            for c in [first] + calls:
                t0 = time.time()
                res = await s.call_tool(c["tool"], c.get("args", {}))
                dt = time.time() - t0
                out_txt = []
                nbytes = 0
                for i, item in enumerate(res.content):
                    if item.type == "text":
                        out_txt.append(item.text); nbytes += len(item.text)
                    elif item.type == "image":
                        data = base64.b64decode(item.data)
                        p = Path(project) / "out" / f"mcp_img_{c['tool']}_{int(time.time())}_{i}.png"
                        p.parent.mkdir(exist_ok=True, parents=True)
                        p.write_bytes(data); nbytes += len(item.data)
                        out_txt.append(f"<image saved {p}>")
                txt = "\n".join(out_txt)
                Path(project).mkdir(parents=True, exist_ok=True)
                with open(log, "a") as f:
                    f.write(json.dumps({"tool": c["tool"], "args": c.get("args", {}), "s": round(dt, 2),
                                        "resp_bytes": nbytes, "is_error": bool(getattr(res, "isError", False))}) + "\n")
                print(f"=== {c['tool']} ({dt:.1f}s, {nbytes}B)")
                print(txt if len(txt) <= maxlen else txt[:maxlen] + f"... [{len(txt)} chars]")


if __name__ == "__main__":
    src = sys.argv[2]
    calls = json.load(sys.stdin if src == "-" else open(src))
    asyncio.run(main(str(Path(sys.argv[1]).resolve()), calls, int(sys.argv[3]) if len(sys.argv) > 3 else 4000))
