#!/usr/bin/env python3
"""
Agent Manager (tmux-based)
============================
Start, stop, list, monitor, and assign tasks to named CLI agents
running in isolated tmux sessions.

Agents are defined in ~/agent-hub/agents/ as JSON files.

Usage:
  python3 agent_manager.py list
  python3 agent_manager.py start nemotron
  python3 agent_manager.py start qwen
  python3 agent_manager.py stop nemotron
  python3 agent_manager.py monitor nemotron
  python3 agent_manager.py assign nemotron "Write a Python web scraper"
  python3 agent_manager.py status
"""

import subprocess
import sys
import json
import os
import argparse
import glob
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
AGENTS_DIR = str(BASE_DIR / "agents")
TMUX_PREFIX = "agent_"

# -- Built-in Agent Definitions -----------------------------------------------
BUILTIN_AGENTS = {
    "nemotron": {
        "name": "nemotron",
        "description": "Nemotron 3 Ultra via OpenRouter (550B cloud model)",
        "command": (
            "OPENAI_API_KEY=$OPENROUTER_API_KEY "
            "OPENAI_API_BASE=https://openrouter.ai/api/v1 "
            "interpreter --model openai/nvidia/nemotron-3-ultra-550b-a55b:free"
        ),
    },
    "qwen": {
        "name": "qwen",
        "description": "Qwen 2.5 Coder 7B local via Ollama (offline, fast)",
        "command": (
            "OPENAI_API_BASE=http://localhost:11434/v1 "
            "OPENAI_API_KEY=ollama "
            "interpreter --model openai/qwen2.5-coder:7b"
        ),
    },
    "router": {
        "name": "router",
        "description": "Smart hybrid router (auto local/cloud)",
        "command": f"python3 {BASE_DIR}/smart_router.py --interactive",
    },
    "parallel": {
        "name": "parallel",
        "description": "Parallel AI multi-engine orchestrator",
        "command": f"python3 {BASE_DIR}/parallel_router.py --interactive",
    },
    "agy": {
        "name": "agy",
        "description": "AGY multi-agent orchestrator",
        "command": f"python3 {BASE_DIR}/agy_orchestrator.py --interactive",
    },
}

# -- tmux Helpers -------------------------------------------------------------
def session_name(agent_name: str) -> str:
    return f"{TMUX_PREFIX}{agent_name}"

def session_exists(agent_name: str) -> bool:
    sn = session_name(agent_name)
    result = subprocess.run(
        ["tmux", "has-session", "-t", sn],
        capture_output=True
    )
    return result.returncode == 0

def running_sessions() -> list:
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    return [
        s.removeprefix(TMUX_PREFIX)
        for s in result.stdout.strip().splitlines()
        if s.startswith(TMUX_PREFIX)
    ]

def get_agent(name: str) -> dict | None:
    if name in BUILTIN_AGENTS:
        return BUILTIN_AGENTS[name]
    agent_file = os.path.join(AGENTS_DIR, f"{name}.json")
    if os.path.exists(agent_file):
        with open(agent_file) as f:
            return json.load(f)
    return None

# -- Commands -----------------------------------------------------------------
def cmd_list(args):
    print("\nBuilt-in Agents:")
    print(f"  {'NAME':<12} {'STATUS':<10} DESCRIPTION")
    print("  " + "-" * 55)
    active = running_sessions()
    for name, info in BUILTIN_AGENTS.items():
        status = "[RUNNING]" if name in active else "[stopped]"
        print(f"  {name:<12} {status:<10} {info['description']}")

    custom_files = glob.glob(os.path.join(AGENTS_DIR, "*.json"))
    if custom_files:
        print("\nCustom Agents:")
        for f in custom_files:
            name = os.path.splitext(os.path.basename(f))[0]
            with open(f) as fh:
                data = json.load(fh)
            status = "[RUNNING]" if name in active else "[stopped]"
            print(f"  {name:<12} {status:<10} {data.get('description','')}")
    print()

def cmd_start(args):
    agent = get_agent(args.agent)
    if not agent:
        print(f"[!] Unknown agent: {args.agent}")
        print(f"    Available: {', '.join(BUILTIN_AGENTS.keys())}")
        sys.exit(1)

    sn = session_name(args.agent)
    if session_exists(args.agent):
        print(f"[!] Agent '{args.agent}' is already running in tmux session: {sn}")
        print(f"    Attach with: tmux attach -t {sn}")
        return

    cmd = agent["command"]
    subprocess.run(["tmux", "new-session", "-d", "-s", sn, "-x", "220", "-y", "50"])
    subprocess.run(["tmux", "send-keys", "-t", sn, cmd, "Enter"])
    print(f"[+] Started '{args.agent}' in tmux session: {sn}")
    print(f"    Attach  : tmux attach -t {sn}")
    print(f"    Detach  : Ctrl-B then D (while inside)")
    print(f"    Monitor : python3 ~/agent-hub/agent_manager.py monitor {args.agent}")

def cmd_stop(args):
    if not session_exists(args.agent):
        print(f"[!] Agent '{args.agent}' is not running.")
        return
    sn = session_name(args.agent)
    subprocess.run(["tmux", "kill-session", "-t", sn])
    print(f"[-] Stopped agent '{args.agent}' (tmux session {sn} killed).")

def cmd_monitor(args):
    if not session_exists(args.agent):
        print(f"[!] Agent '{args.agent}' is not running. Start it first:")
        print(f"    python3 ~/agent-hub/agent_manager.py start {args.agent}")
        return
    sn = session_name(args.agent)
    print(f"[*] Attaching to agent '{args.agent}' (Ctrl-B D to detach)...")
    subprocess.run(["tmux", "attach", "-t", sn])

def cmd_assign(args):
    if not session_exists(args.agent):
        print(f"[!] Agent '{args.agent}' is not running. Starting it first...")
        cmd_start(argparse.Namespace(agent=args.agent))
        import time; time.sleep(2)

    sn = session_name(args.agent)
    task = args.task
    subprocess.run(["tmux", "send-keys", "-t", sn, task, "Enter"])
    print(f"[>] Assigned task to '{args.agent}': {task[:80]}")

def cmd_status(args):
    active = running_sessions()
    if not active:
        print("\n[*] No agents currently running.\n")
        return
    print(f"\n[*] Running agents ({len(active)}):")
    for name in active:
        agent = get_agent(name)
        desc = agent["description"] if agent else "(custom agent)"
        print(f"    - {name:<12}  {desc}")
        print(f"      Attach: tmux attach -t {session_name(name)}")
    print()

# -- CLI ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="agent_manager",
        description="Manage local CLI agents in tmux sessions",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list",   help="List all agents and their status")
    sub.add_parser("status", help="Show currently running agents")

    p_start = sub.add_parser("start", help="Start an agent in a tmux session")
    p_start.add_argument("agent", help="Agent name (nemotron|qwen|router|agy)")

    p_stop = sub.add_parser("stop", help="Stop a running agent")
    p_stop.add_argument("agent")

    p_mon = sub.add_parser("monitor", help="Attach to an agent session")
    p_mon.add_argument("agent")

    p_assign = sub.add_parser("assign", help="Send a task to a running agent")
    p_assign.add_argument("agent")
    p_assign.add_argument("task", help="The task/prompt to send")

    args = parser.parse_args()
    if not args.command:
        parser.print_help(); sys.exit(0)

    dispatch = {
        "list":    cmd_list,
        "status":  cmd_status,
        "start":   cmd_start,
        "stop":    cmd_stop,
        "monitor": cmd_monitor,
        "assign":  cmd_assign,
    }
    dispatch[args.command](args)

if __name__ == "__main__":
    main()
