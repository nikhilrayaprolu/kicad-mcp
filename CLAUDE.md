# Pcb-mcp

This repository is an MCP server for AI-driven PCB design. When designing a board with the `pcb-mcp` tools, follow
@AGENTS.md exactly. It is the playbook: verification gates, netclass and escape rules, floorplan intent,
the hybrid routing portfolio, systematic closing of the last connections, length tuning, and honest reporting.

Development: `source tools/env.sh && pixi run test`. Regenerate `docs/TOOLS.md` with `pixi run python clients/gen_tool_docs.py`
after changing any tool. Never commit `tools/kicad-root`, `.pixi`, `router_cpp/hroute` (binary) or `designs/*/out/{route,hroute,hybrid,repair,placements}`.
