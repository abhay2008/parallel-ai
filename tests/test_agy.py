import asyncio
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

async def main():
    try:
        from google.antigravity import Agent, LocalAgentConfig
        gemini_key = os.environ.get("GEMINI_API_KEY", None)
        async with Agent(config=LocalAgentConfig(system_instructions="say hi", api_key=gemini_key)) as agent:
            resp = await agent.chat("hi")
            print("Response:", await resp.text())
    except ImportError:
        print("[!] google-antigravity SDK not installed or available.")

if __name__ == "__main__":
    asyncio.run(main())
