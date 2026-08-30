#!/usr/bin/env python3
"""
Smart Hybrid Router v2
========================
Routes prompts across THREE tiers:

  SYSTEM -> open-interpreter (local Qwen OR cloud Nemotron)
            Triggers when task needs real shell/system access.
            Always asks permission before running ANY code.

  CLOUD  -> Nemotron 3 Ultra via OpenRouter (litellm chat)
            For complex reasoning, architecture, deep research.

  LOCAL  -> Qwen 2.5 Coder via Ollama (litellm chat)
            Fast, private, offline. Simple Q&A and short code.

Routing Decision Order:
  1. System-access keywords present?      -> SYSTEM (open-interpreter)
  2. Prompt length > 400 chars?           -> CLOUD
  3. Complexity/research keywords?        -> CLOUD
  4. Anything else                        -> LOCAL

Usage:
  python3 smart_router.py --interactive
  python3 smart_router.py "your prompt"
  python3 smart_router.py --explain "your prompt"
"""

import os
import sys
import subprocess
import argparse
import termios
import litellm

litellm.suppress_debug_info = True

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# -- Config -------------------------------------------------------------------
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CLOUD_MODEL    = "openai/nvidia/nemotron-3-ultra-550b-a55b:free"
CLOUD_API_BASE = os.environ.get("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
LOCAL_MODEL    = "openai/qwen2.5-coder:7b"
LOCAL_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434/v1")
LOCAL_API_KEY  = "ollama"

CLOUD_PROMPT_LEN = 400

# Keywords that signal the task needs REAL system access (shell execution)
SYSTEM_KEYWORDS = {
    # file/disk operations
    "installed", "libraries", "packages", "dependencies", "check my",
    "list my", "show my", "what's on my", "whats on my", "on my computer",
    "on my machine", "on my system", "my mac", "my laptop", "my desktop",
    "my files", "my disk", "my drive", "disk space", "storage",
    # process/system inspection
    "running", "processes", "cpu", "memory", "ram", "port",
    "network", "firewall", "permissions", "chmod", "chown",
    # package managers
    "brew", "pip list", "npm list", "conda list", "gem list",
    "pip freeze", "yarn list", "cargo list",
    # discrepancy / verification
    "discrepanc", "mismatch", "conflict", "version mismatch",
    "verify", "validate", "audit my", "inspect my",
    # execution words
    "run this", "execute", "install", "uninstall", "upgrade my",
    "update my", "fix my", "repair my", "clean up",
}

# Keywords that signal heavy cloud-level reasoning (no system access needed)
CLOUD_KEYWORDS = {
    "architect", "architecture", "design", "scalab",
    "microservic", "distributed", "security audit", "refactor",
    "complex", "research", "analyze", "strategy",
    "compare", "evaluate", "tradeoff", "trade-off", "production",
    "kubernetes", "docker", "terraform", "infra",
    "deep", "comprehensive", "detailed", "thorough",
    "explain why", "how does", "what is the best",
}

# -- Routing Logic ------------------------------------------------------------
def decide_route(prompt: str) -> tuple:
    """Returns (destination, reason): 'system', 'cloud', or 'local'."""
    pl = prompt.lower()

    # Tier 1: Needs real system access
    sys_matched = [kw for kw in SYSTEM_KEYWORDS if kw in pl]
    if sys_matched:
        return "system", f"system-access keywords: {', '.join(sys_matched[:3])}"

    # Tier 2: Complex reasoning -> cloud chat
    if len(prompt) > CLOUD_PROMPT_LEN:
        return "cloud", f"prompt length ({len(prompt)} chars) > {CLOUD_PROMPT_LEN}"
    cloud_matched = [kw for kw in CLOUD_KEYWORDS if kw in pl]
    if cloud_matched:
        return "cloud", f"complexity keywords: {', '.join(cloud_matched[:3])}"

    # Tier 3: Fast local
    return "local", "simple prompt -> fast local inference"

# -- Permission Gate ----------------------------------------------------------
def ask_permission(destination: str, prompt: str) -> bool:
    """Ask user before routing to open-interpreter (system access)."""
    # Flush stdin to prevent multi-line pasted text from instantly cancelling the prompt
    try:
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass
        
    print("\n" + "="*60)
    print("  SYSTEM ACCESS REQUIRED")
    print("="*60)
    print(f"  Task   : {prompt[:80]}{'...' if len(prompt)>80 else ''}")
    print(f"  Action : open-interpreter will run shell commands on your Mac")
    print(f"  Model  : Nemotron 3 Ultra (cloud) via OpenRouter")
    print("="*60)
    try:
        answer = input("  Allow? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return False
    return answer in {"y", "yes"}

# -- Executors ----------------------------------------------------------------
def call_cloud(messages: list) -> str:
    resp = litellm.completion(
        model=CLOUD_MODEL,
        messages=messages,
        api_key=OPENROUTER_KEY,
        api_base=CLOUD_API_BASE,
    )
    return resp.choices[0].message.content

def call_local(messages: list) -> str:
    try:
        resp = litellm.completion(
            model=LOCAL_MODEL,
            messages=messages,
            api_key=LOCAL_API_KEY,
            api_base=LOCAL_API_BASE,
        )
        return resp.choices[0].message.content
    except Exception as e:
        print(f"\n[!] Local model unavailable ({e}). Falling back to cloud...\n")
        return call_cloud(messages)

def call_system(prompt: str) -> None:
    """Launch open-interpreter with Nemotron for system-access tasks."""
    import shutil
    import re
    
    env = os.environ.copy()
    env["OPENAI_API_KEY"]  = OPENROUTER_KEY
    env["OPENAI_API_BASE"] = CLOUD_API_BASE
    
    interpreter_bin = shutil.which("interpreter")
    python_bin = "python3"
    if interpreter_bin:
        try:
            with open(interpreter_bin, "r") as f:
                content = f.read()
                m = re.search(r"'''exec'\s+'([^']+)'", content)
                if m:
                    python_bin = m.group(1)
        except Exception:
            pass

    code = """
import sys
from interpreter import interpreter
interpreter.llm.model = sys.argv[1]
interpreter.auto_run = True
interpreter.chat(sys.argv[2])
"""
    print(f"\n[*] Launching open-interpreter (Nemotron)...\n")
    subprocess.run([python_bin, "-c", code, CLOUD_MODEL, prompt], env=env)

# -- Main Router --------------------------------------------------------------
def route(prompt: str, history: list = None, verbose: bool = True) -> str | None:
    destination, reason = decide_route(prompt)
    messages = (history or []) + [{"role": "user", "content": prompt}]

    tier_labels = {
        "system": "SYSTEM  (open-interpreter + Nemotron)",
        "cloud":  "CLOUD   (Nemotron via OpenRouter)",
        "local":  "LOCAL   (Qwen 2.5 Coder via Ollama)",
    }
    if verbose:
        print(f"\n>> Routing -> {tier_labels[destination]}")
        print(f"   Reason  : {reason}\n")

    if destination == "system":
        if not ask_permission(destination, prompt):
            print("\n[x] Cancelled. Type 'quit' to exit or try rephrasing.\n")
            return None
        call_system(prompt)
        # Manually save to history so the next prompt retains context of this action
        if history is not None:
            history.append({"role": "user", "content": prompt})
            history.append({"role": "assistant", "content": "[System task executed via open-interpreter]"})
        return None  # open-interpreter handles its own output

    if destination == "cloud":
        return call_cloud(messages)

    return call_local(messages)

# -- CLI ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="smart_router",
        description="3-tier AI router: system | cloud | local",
    )
    parser.add_argument("prompt", nargs="?", help="Prompt to route and run")
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="Start interactive REPL session")
    parser.add_argument("--explain", "-e", action="store_true",
                        help="Show routing decision only, no model call")
    args = parser.parse_args()

    if args.interactive:
        print("Smart Hybrid Router v2  |  3 Tiers: system / cloud / local")
        print("Type 'quit' or Ctrl-C to exit.\n")
        history = []
        try:
            while True:
                user_input = input("You: ").strip()
                if not user_input or user_input.lower() in {"quit", "exit", "q"}:
                    break
                answer = route(user_input, history=history)
                if answer is not None:
                    history.append({"role": "user",      "content": user_input})
                    history.append({"role": "assistant",  "content": answer})
                    print(f"\nAssistant: {answer}\n")
        except KeyboardInterrupt:
            print("\nGoodbye!")
        return

    if args.prompt:
        if args.explain:
            dest, reason = decide_route(args.prompt)
            labels = {"system": "SYSTEM (open-interpreter)", "cloud": "CLOUD (Nemotron)", "local": "LOCAL (Qwen)"}
            print(f"Route  : {labels[dest]}")
            print(f"Reason : {reason}")
        else:
            answer = route(args.prompt)
            if answer:
                print(answer)
        return

    parser.print_help()

if __name__ == "__main__":
    main()
