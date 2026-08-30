import asyncio
import os
import sys
import litellm

litellm.suppress_debug_info = True

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CLOUD_API_BASE = os.environ.get("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
DEFAULT_CLOUD_MODEL = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"

async def main():
    if not OPENROUTER_KEY:
        print("[!] OPENROUTER_API_KEY is not set.")
        return

    messages = [
        {"role": "system", "content": "You are Nemotron."},
        {"role": "user", "content": "Explain parallel agent routing in 2 sentences."}
    ]
    resp = await litellm.acompletion(
        model=DEFAULT_CLOUD_MODEL,
        messages=messages,
        api_key=OPENROUTER_KEY,
        api_base=CLOUD_API_BASE
    )
    print("Response:", resp.choices[0].message.content)

if __name__ == "__main__":
    asyncio.run(main())
