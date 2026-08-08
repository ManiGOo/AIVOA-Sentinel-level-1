import asyncio
from mcp import ClientSession
from mcp.client.sse import sse_client

async def main():
    print("Connecting to MCP Streamable HTTP server at http://localhost:5000/mcp...")
    async with sse_client("http://localhost:5000/mcp") as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            print("Initializing session...")
            await session.initialize()
            print("Listing tools...")
            tools = await session.list_tools()
            print("Received tools:")
            for tool in tools.tools:
                print(f"  - {tool.name}: {tool.description}")

if __name__ == "__main__":
    asyncio.run(main())
