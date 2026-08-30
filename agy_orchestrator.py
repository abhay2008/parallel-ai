#!/usr/bin/env python3
"""
AGY Multi-Agent Orchestrator
==============================
Spawns specialized subagents via the Google Antigravity SDK:
  - coordinator: decides task routing
  - code_agent:  writes/reviews code (uses local Qwen via Ollama)
  - cloud_agent: handles complex architecture/research (uses Gemini)

Usage:
  python3 agy_orchestrator.py "your task"
  python3 agy_orchestrator.py --interactive
"""

import asyncio
import os
import sys
import argparse
from google.antigravity import Agent, LocalAgentConfig, LocalOpenAIAgentConfig, types

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

# -- Agent Configs -----------------------------------------------------------

def make_coordinator_config():
    """High-intelligence coordinator - decides task routing."""
    return LocalAgentConfig(
        api_key=GEMINI_KEY or None,
        system_instructions="""You are a coordinator agent. When given a task:
1. If it involves writing/editing code, running scripts, or small quick tasks -> delegate to the code_agent subagent.
2. If it involves architecture, design, research, or complex multi-step reasoning -> handle it yourself with full detail.
Always be explicit about which path you chose and why.""",
        capabilities=types.CapabilitiesConfig(
            enable_subagents=True,
        ),
    )

def make_code_agent_config():
    """Local lightweight agent for coding tasks."""
    return LocalOpenAIAgentConfig(
        model="qwen2.5-coder:7b",
        base_url="http://localhost:11434/v1",
        system_instructions="""You are a coding assistant specialized in Python, Bash, and JavaScript.
Write clean, well-commented, working code. Always explain what the code does briefly.""",
    )

# -- Main Orchestrator -------------------------------------------------------

async def run_coordinator(prompt: str):
    print(f"\n[AGY Orchestrator] Processing: {prompt[:80]}...\n")

    if not GEMINI_KEY:
        print("[!] GEMINI_API_KEY not set. Running in local-only mode (Qwen).")
        config = make_code_agent_config()
        async with Agent(config=config) as agent:
            response = await agent.chat(prompt)
            print(f"\nAgent: {await response.text()}")
        return

    config = make_coordinator_config()
    async with Agent(config=config) as coordinator:
        response = await coordinator.chat(prompt)
        print(f"\nCoordinator: {await response.text()}")

async def interactive_session():
    print("AGY Multi-Agent Orchestrator -- Interactive Mode")
    print("Type 'quit' to exit.\n")
    while True:
        try:
            task = input("Task: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break
        if task.lower() in {"quit", "exit", "q"}:
            break
        if task:
            await run_coordinator(task)

# -- CLI ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="agy_orchestrator",
        description="AGY multi-agent orchestrator: smart task routing",
    )
    parser.add_argument("task", nargs="?", help="Task to run")
    parser.add_argument("--interactive", "-i", action="store_true")
    args = parser.parse_args()

    if args.interactive:
        asyncio.run(interactive_session())
    elif args.task:
        asyncio.run(run_coordinator(args.task))
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
