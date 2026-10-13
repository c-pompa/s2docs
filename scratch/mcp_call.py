#!/usr/bin/env python3
"""Minimal s2docs MCP client: mcp_call.py <tool> '<json-args>'"""
import asyncio, json, sys
from mcp import Client

URL = "http://127.0.0.1:8812/mcp"

async def main():
    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    async with Client(URL) as c:
        res = await c.call_tool(tool, arguments=args)
        for block in res.content:
            text = getattr(block, "text", None)
            if text is not None:
                print(text)

asyncio.run(main())
