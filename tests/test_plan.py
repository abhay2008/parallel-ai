import asyncio
import json
import shutil
import os

async def call_agy_cli(prompt: str, json_mode: bool = False) -> str:
    agy_bin = shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
    args = [agy_bin, "-p", prompt, "--dangerously-skip-permissions"]
    if json_mode:
        args.extend(["--output-format", "json"])
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    print("STDOUT:", stdout.decode('utf-8'))
    if stderr:
        print("STDERR:", stderr.decode('utf-8'))
    return stdout.decode('utf-8').strip()

async def main():
    planning_prompt = """Analyze the user's input and decide how to route it.
User Input: "hey who is this"

Respond ONLY with a valid JSON object matching this schema:
{
  "is_simple_chat": boolean, // true if it's just a greeting or simple reply like "yessir"
  "direct_response": string, // if is_simple_chat is true, provide the response here, else empty string
  "needs_deep_analysis": boolean, // true if it requires architectural, theoretical, or complex logic breakdown
  "needs_code": boolean // true if it asks for scripts, code, or terminal commands
}"""
    await call_agy_cli(planning_prompt, json_mode=True)

if __name__ == "__main__":
    asyncio.run(main())
