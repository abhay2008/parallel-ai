#!/usr/bin/env python3
"""
Parallel Agent Router
======================
Executes a prompt across three different AI engines SIMULTaneously:
1. AGY (Logic Handler)   -> Focuses on strategy, task breakdown, and planning.
2. Nemotron (Deep Cloud) -> Focuses on heavy analysis and deep explanations.
3. Qwen (Local Coder)    -> Spun up locally just to write the code/scripts.

Usage:
  python3 parallel_router.py "your complex task"
"""

import asyncio
import os
import sys
import json
import argparse
import urllib.request
import tempfile
import re
import ast
from pathlib import Path
import litellm

litellm.suppress_debug_info = True

import shutil
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CLOUD_API_BASE = os.environ.get("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
LOCAL_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434/v1")

DEFAULT_CLOUD_MODEL = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
LOCAL_MODEL = "openai/qwen2.5-coder:7b"

active_model = DEFAULT_CLOUD_MODEL
thinking_level = "Auto"

session_tokens = 0
session_requests = 0

# ─── Self-Healing Execution Config ────────────────────────────────────────────
MAX_REPAIR_ATTEMPTS = 4      # Extra attempt buffer for complex async tasks
EXECUTION_TIMEOUT   = 60.0  # Seconds per subprocess run (bumped from 30s based on marathon data)

def check_usage():
    print("\n📊 Checking OpenRouter & AGY Token Usage...")
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/auth/key", 
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"}
        )
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode()).get('data', {})
            limit = data.get('limit')
            usage = data.get('usage', 0)
            if limit is not None:
                limit_float = float(limit)
                limit_str = f"${limit_float:.4f}"
            else:
                limit_str = "Unlimited"
                
            is_free = data.get('is_free_tier', False)
            print(f"   OpenRouter Cost: ${float(usage):.6f} / {limit_str}")
            print(f"   AGY Session Tokens Used (Gemini): {session_tokens}")
            print(f"   AGY Session Requests: {session_requests}")
            
            print("\n   [Limits & Replenishment Info]")
            print("   - OpenRouter Paid Credits: Do not automatically replenish. Must be topped up at openrouter.ai.")
            print("   - Nemotron Free Tier: 200 requests per day limit. Resets daily.")
            
            if limit is not None and float(usage) >= float(limit):
                print("\n   ⚠️ WARNING: You have reached your paid tier limit!")
            else:
                print("\n   ✅ You have OpenRouter credits/allowance remaining.")
    except Exception as e:
        print(f"   [!] Usage check failed: {repr(e)}")
    print("="*60 + "\n")

async def call_agy_cli(prompt: str, json_mode: bool = False) -> str:
    """Helper to call the native AGY CLI binary for Gemini intelligence."""
    agy_bin = shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
    args = [agy_bin, "-p", prompt, "--dangerously-skip-permissions"]
    if json_mode:
        args.extend(["--output-format", "json"])
    
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    return stdout.decode('utf-8').strip()

FALLBACK_CLOUD_MODELS = [
    "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "openrouter/meta-llama/llama-3.3-70b-instruct:free",
    "openrouter/deepseek/deepseek-r1:free"
]

async def run_nemotron_heavy(prompt: str) -> str:
    global session_tokens, session_requests
    messages = [
        {"role": "system", "content": f"You are Nemotron. Provide a deep, comprehensive analysis for the user's request. Focus on 'Why' and 'How'. Current Thinking Level requested: {thinking_level}"},
        {"role": "user", "content": prompt}
    ]
    
    models_to_try = [active_model] + [m for m in FALLBACK_CLOUD_MODELS if m != active_model]
    
    last_err = None
    for model_candidate in models_to_try:
        try:
            resp = await asyncio.wait_for(
                litellm.acompletion(
                    model=model_candidate,
                    messages=messages,
                    api_key=OPENROUTER_KEY,
                    api_base=CLOUD_API_BASE
                ),
                timeout=90.0
            )
            if hasattr(resp, "usage") and resp.usage:
                session_tokens += resp.usage.total_tokens
            session_requests += 1
            return resp.choices[0].message.content
        except asyncio.TimeoutError:
            print(f"⚠️ Cloud model {model_candidate} timed out! Trying fallback...")
            last_err = "Timeout"
        except Exception as e:
            print(f"⚠️ Cloud model {model_candidate} failed: {e}. Trying fallback...")
            last_err = str(e)
            
    print("⚠️ All OpenRouter models exhausted or rate-limited. Falling back to AGY (Gemini) for deep analysis...")
    analysis_prompt = f"Provide a deep, comprehensive architectural analysis for the user's request. Focus on 'Why' and 'How'.\n\nPrompt: {prompt}"
    try:
        raw_agy = await call_agy_cli(analysis_prompt, json_mode=True)
        try:
            outer = json.loads(raw_agy)
            return outer.get("response", "").strip()
        except Exception:
            return raw_agy.strip()
    except Exception as e:
        return f"[Failed to reach both OpenRouter and AGY fallback: {e}]"

async def run_qwen_local(prompt: str) -> str:
    messages = [
        {"role": "system", "content": "You are Qwen, a fast local coding assistant. Provide ONLY the code snippet or terminal commands needed for this request."},
        {"role": "user", "content": prompt}
    ]
    try:
        resp = await asyncio.wait_for(
            litellm.acompletion(
                model=LOCAL_MODEL,
                messages=messages,
                api_key="ollama",
                api_base=LOCAL_API_BASE
            ),
            timeout=90.0
        )
        return resp.choices[0].message.content
    except asyncio.TimeoutError:
        return f"⚠️ Subagent {LOCAL_MODEL} timed out after 90s! Falling back..."
    except Exception as e:
        return f"[Failed to reach local Ollama: {e}]"

# ─── Self-Healing Utilities ───────────────────────────────────────────────────

def extract_python_code(llm_response: str) -> str:
    """Extracts the largest Python code block, ignoring markdown text/diagrams."""
    blocks = re.findall(r"```python\n(.*?)\n```", llm_response, re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip()
    # Fallback: generic fences
    blocks_generic = re.findall(r"```\n(.*?)\n```", llm_response, re.DOTALL)
    if blocks_generic:
        return max(blocks_generic, key=len).strip()
    return llm_response.strip()


def ast_validate(code: str) -> tuple[bool, str]:
    """Pre-flight AST syntax check — catches SyntaxErrors before spending subprocess time."""
    try:
        ast.parse(code)
        return True, "AST OK"
    except SyntaxError as e:
        return False, f"SyntaxError at line {e.lineno}: {e.msg}\n  → {e.text}"


async def execute_in_subprocess(script_code: str, timeout_sec: float = EXECUTION_TIMEOUT) -> tuple[bool, str]:
    """
    Executes generated Python code in an isolated subprocess.
    Returns (success: bool, output_or_traceback: str).
    """
    # Step 0: fast AST syntax gate
    ast_ok, ast_msg = ast_validate(script_code)
    if not ast_ok:
        return False, f"AST SYNTAX VALIDATION FAILED (no subprocess spawned):\n{ast_msg}"

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8") as tmp:
        tmp.write(script_code)
        tmp_path = tmp.name

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            if proc.returncode == 0:
                return True, stdout_str or "Execution completed cleanly with exit code 0."
            else:
                error_log = (
                    f"RETURN CODE: {proc.returncode}\n\n"
                    f"STDERR TRACEBACK:\n{stderr_str}\n\n"
                    f"STDOUT LOGS:\n{stdout_str}"
                )
                return False, error_log

        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return False, f"EXECUTION TIMED OUT after {timeout_sec}s! Possible infinite loop or blocking call."
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# Known noise strings that are not real exceptions - filter these out of learning
_NOISE_PATTERNS = frozenset([
    "STDOUT LOGS:", "STDERR TRACEBACK:", "RETURN CODE:",
    "EXECUTION TIMED OUT", "AST SYNTAX VALIDATION FAILED"
])

def get_learned_rules() -> str:
    """
    Loads dynamically discovered pitfalls from learned_patterns.json.
    Returns only real exception-class rules (filters noise).
    """
    pat_file = Path(__file__).resolve().parent / "learned_patterns.json"
    if not pat_file.exists():
        return ""
    try:
        data = json.loads(pat_file.read_text(encoding="utf-8"))
        rules = data.get("learned_rules", [])
        # Filter out noisy / duplicate rules
        seen = set()
        clean = []
        for r in rules:
            key = r.strip()
            if key not in seen and not any(n in key for n in _NOISE_PATTERNS):
                seen.add(key)
                clean.append(r)
        if not clean:
            return ""
        bullet_list = "\n".join(f"- {r}" for r in clean[-12:])
        return f"\n### DYNAMICALLY LEARNED PITFALLS FROM PREVIOUS RUNS (DO NOT REPEAT THESE):\n{bullet_list}\n"
    except Exception:
        return ""

def record_learned_pitfall(error_logs: str):
    """
    Extracts the actual Python exception class + message from a traceback
    and saves it to learned_patterns.json for future repair prompt injection.
    Skips generic noise lines like 'STDOUT LOGS:' that aren't real exceptions.
    """
    pat_file = Path(__file__).resolve().parent / "learned_patterns.json"
    try:
        data = {"learned_rules": []}
        if pat_file.exists():
            data = json.loads(pat_file.read_text(encoding="utf-8"))

        # Hunt for lines starting with known Python exception class names
        exc_line = None
        for line in error_logs.splitlines():
            stripped = line.strip()
            # Real exception lines look like: "ValueError: ...", "RuntimeError: ...", etc.
            if stripped and ":" in stripped:
                prefix = stripped.split(":")[0].strip()
                if (
                    prefix[0].isupper()                        # CamelCase = exception class
                    and " " not in prefix                      # single word
                    and any(suffix in prefix for suffix in [
                        "Error", "Warning", "Exception", "Timeout",
                        "Interrupt", "Failure", "Overflow"
                    ])
                    and not any(n in stripped for n in _NOISE_PATTERNS)
                ):
                    exc_line = stripped[:160]  # cap length

        if not exc_line:
            return  # Nothing actionable to learn

        rule = f"Fix pattern: {exc_line}"
        existing = data.get("learned_rules", [])
        # Deduplicate by exception class prefix
        exc_class = exc_line.split(":")[0]
        if not any(exc_class in r for r in existing):
            existing.append(rule)
            data["learned_rules"] = existing
            pat_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(f"💡 [PAI Learning] Recorded: {rule[:100]}")
    except Exception as e:
        print(f"⚠️ [PAI Learning] Failed to save pitfall rule: {e}")

async def self_healing_orchestrator(original_prompt: str, initial_synthesized_code: str) -> tuple[str, str]:
    """
    Execute → Validate → Repair loop with Dynamic Pattern Memory.
    Tries up to MAX_REPAIR_ATTEMPTS to fix the generated code by feeding
    the exact traceback back to Qwen for a targeted patch.
    Returns (final_code: str, final_log: str).
    """
    current_code = extract_python_code(initial_synthesized_code)

    # ── Ensure executable harness exists ──────────────────────────────────────
    if not ("if __name__" in current_code or "asyncio.run" in current_code or "unittest.main()" in current_code):
        print("⚡ [PAI Self-Heal] Code lacks executable harness. Dispatching to Qwen to add test runner...")
        harness_prompt = f"""You are a Python Test Automation Specialist. The generated code below is a library module without an execution harness.

### CODE:
```python
{current_code}
```

### INSTRUCTION:
Append a complete `if __name__ == '__main__':` test block at the end that thoroughly exercises all classes/methods, runs concurrent operations or assertions, and prints '✅ Test Passed'.
Return ONLY the full updated code inside standard markdown fences (```python ... ```).
"""
        harness_resp = await run_qwen_local(harness_prompt)
        current_code = extract_python_code(harness_resp)

    for attempt in range(1, MAX_REPAIR_ATTEMPTS + 1):
        print(f"\n⚡ [PAI Self-Heal] Subprocess check (Attempt {attempt}/{MAX_REPAIR_ATTEMPTS})...")

        success, logs = await execute_in_subprocess(current_code)

        if success:
            print(f"✅ [PAI Self-Heal] Code passed on attempt {attempt}!")
            print(f"   Output: {logs[:300]}{'...' if len(logs) > 300 else ''}")
            return current_code, logs

        print(f"❌ [PAI Self-Heal] Failed. Dispatching traceback to Qwen for repair...")
        print(f"   Error: {logs[:400]}{'...' if len(logs) > 400 else ''}")
        
        # Learn from the failure
        record_learned_pitfall(logs)

        learned_context = get_learned_rules()
        repair_prompt = f"""You are an expert Python Asyncio System Engineer. The generated script failed execution.

### TRACEBACK & EXECUTION FAILURE LOGS:
{logs}

### CURRENT BROKEN CODE:
```python
{current_code}
```

### ORIGINAL USER PROMPT:
{original_prompt}
{learned_context}
### REPAIR RULES (derived from 186-run marathon failure analysis):
- Fix the EXACT error shown in the traceback. Do NOT rewrite the whole program.
- asyncio.start_server() MUST be AWAITED — use `server = await asyncio.start_server(...)`.
- NEVER use `server.serve_forever()` — use `async with server: await asyncio.gather(*tasks)`.
- All struct.pack/unpack CRC32 values MUST use unsigned format `!I` (not signed `i`).
- Use `asyncio.Event` + `asyncio.Queue` for producer/consumer coordination — NOT `threading.Event`.
- In thread-based tests, use `threading.Event` for shutdown signals — NOT asyncio primitives.
- For concurrent HashTable, use `threading.RLock()` per stripe, NOT a global `threading.Lock()`.
- For asyncio TCP clients, always add `await asyncio.sleep(0)` yields to prevent starvation.
- Do NOT call `loop.run_until_complete()` inside `async def` — use `await` directly.
- Actor mailboxes: use `asyncio.Queue(maxsize=0)` (unbounded) to prevent deadlock on crash simulation.
- Priority queues: use `(priority, seqnum, item)` tuples so ties never cause TypeError on item comparison.
- Trie broker: wildcard `#` must match zero or more segments — handle empty suffix case explicitly.
- B-Tree WAL recovery: always flush WAL before mmap.flush() — call `wal_file.flush(); os.fsync()` after writes.
- Delta-of-Delta TSDB: store leading bit count correctly — use bit-shift masks not hardcoded byte boundaries.
- Return ONLY valid Python code inside ```python ... ``` fences.
- Do NOT output ASCII diagrams, box drawings, markdown text, or any content outside the code fence.
"""
        repaired_response = await run_qwen_local(repair_prompt)
        current_code = extract_python_code(repaired_response)

    print(f"⚠️  [PAI Self-Heal] Reached maximum attempts ({MAX_REPAIR_ATTEMPTS}). Returning latest iteration.")
    return current_code, logs

async def parallel_process(prompt: str, history: list):
    # Step 1: AGY decides what to do
    planning_prompt = f"""Analyze the user's input and decide how to route it.
User Input: "{prompt}"

Respond ONLY with a valid JSON object matching this schema:
{{
  "is_simple_chat": boolean, // true ONLY for short greetings like 'hey', 'hi', 'ok'. For ANY questions or instructions, use false.
  "direct_response": string, // if is_simple_chat is true, provide the response here, else empty string
  "needs_deep_analysis": boolean, // true if it requires answering a question, explanation, architecture, theory, or reasoning
  "needs_code": boolean // true if it asks for scripts, code, or terminal commands
}}"""

    global session_tokens, session_requests
    raw_output = await call_agy_cli(planning_prompt, json_mode=True)
    
    try:
        # AGY returns JSON where the actual model text is inside the "response" key
        outer_json = json.loads(raw_output)
        plan_text = outer_json.get("response", "").strip()
        session_tokens += outer_json.get("usage", {}).get("total_tokens", 0)
    except Exception:
        plan_text = raw_output.strip()

    # Strip markdown code blocks if present
    if plan_text.startswith("```json"):
        plan_text = plan_text[7:-3]
    elif plan_text.startswith("```"):
        plan_text = plan_text[3:-3]
        
    try:
        plan = json.loads(plan_text.strip())
    except Exception as e:
        print(f"\n[DEBUG] JSON Parsing Failed. Raw text was: {plan_text}")
        # Fallback if JSON parsing fails
        plan = {"is_simple_chat": False, "needs_deep_analysis": True, "needs_code": True, "direct_response": ""}

    if plan.get("is_simple_chat"):
        print(f"\n🧠 [AGY | {active_model} | Think: {thinking_level}]: {plan.get('direct_response', 'Acknowledged.')}")
        history.append({"role": "assistant", "content": plan.get('direct_response', 'Acknowledged.')})
        return

    print(f"\n🚀 [AGY] Delegating tasks...")
    tasks = []
    if plan.get("needs_deep_analysis"):
        print("   -> ☁️  Spinning up Nemotron for analysis...")
        tasks.append(asyncio.create_task(run_nemotron_heavy(prompt)))
    if plan.get("needs_code"):
        print("   -> 💻 Spinning up Qwen for local code generation...")
        tasks.append(asyncio.create_task(run_qwen_local(prompt)))
        
    if not tasks:
        print("\n🧠 [AGY]: I couldn't determine what subagents to spin up. Could you clarify?")
        return

    # Run required agents in parallel
    results = await asyncio.gather(*tasks)
    
    # Synthesis
    print("\n🧠 [AGY Synthesizing responses...]")
    synthesis_prompt = (
        f"You are AGY, the master coordinator. Synthesize the findings from your subagents into ONE clear, "
        f"unified, and authoritative response to the user. "
        f"CRITICAL INSTRUCTION: If one subagent provided text analysis/architecture and another provided code, "
        f"you MUST include BOTH sections in your final output. Do NOT drop the text analysis under any circumstances. "
        f"User requested Thinking Level: {thinking_level}.\n\n"
        f"History: {history}\n\nUser Prompt: {prompt}\n\nSubagent Data:\n"
        + "\n\n---\n\n".join(results)
    )
    
    raw_synthesis = await call_agy_cli(synthesis_prompt, json_mode=True)
    
    try:
        outer_json = json.loads(raw_synthesis)
        final_text = outer_json.get("response", "").strip()
        session_tokens += outer_json.get("usage", {}).get("total_tokens", 0)
    except Exception:
        final_text = raw_synthesis.strip()

    session_requests += 1
    
    print("\n======================================================================")
    print(f"🧠 [AGY | Model: {active_model} | Think: {thinking_level}]")
    print("----------------------------------------------------------------------")
    print(final_text)
    print("======================================================================\n")
    history.append({"role": "assistant", "content": final_text})

    # ── Self-Healing Gate: only runs if the response contains executable code ──
    if plan.get("needs_code") and "```" in final_text:
        print("\n🔬 [PAI Self-Heal] Detected generated code — entering Execute→Validate→Repair loop...")
        healed_code, exec_log = await self_healing_orchestrator(prompt, final_text)
        # Print the final verified (or best-effort) code block
        print("\n======================================================================")
        print("✅ [PAI Self-Heal] Final Verified Code:")
        print("----------------------------------------------------------------------")
        print(f"```python\n{healed_code}\n```")
        print(f"\n📋 Execution Log:\n{exec_log[:600]}{'...' if len(exec_log) > 600 else ''}")
        print("======================================================================\n")
        # Append the self-healed code to history so future turns have context
        history.append({"role": "assistant", "content": f"[Self-Healed Code]\n```python\n{healed_code}\n```"})

def main():
    parser = argparse.ArgumentParser(description="Parallel Hybrid Orchestrator")
    parser.add_argument("prompt", nargs="?", help="The prompt to execute")
    parser.add_argument("--interactive", "-i", action="store_true", help="Start interactive mode")
    args = parser.parse_args()

    if args.interactive:
        print("AGY Orchestrator | Type 'quit' to exit")
        print("Commands: /usage, /model <name>, /thinking <level>")
        def get_input(prompt_text):
            import sys, select
            print(prompt_text, end='', flush=True)
            first_line = sys.stdin.readline()
            if not first_line:
                raise EOFError
            lines = [first_line]
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if r:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    lines.append(line)
                else:
                    break
            return "".join(lines).strip()

        history = []
        while True:
            try:
                user_input = get_input("\nYou: ")
                if user_input.lower() in {"quit", "exit", "q"}:
                    break
                elif user_input == "/usage":
                    check_usage()
                elif user_input.startswith("/model "):
                    global active_model
                    active_model = user_input.split(" ", 1)[1]
                    print(f"✅ Model set to: {active_model}")
                elif user_input.startswith("/thinking "):
                    global thinking_level
                    thinking_level = user_input.split(" ", 1)[1]
                    print(f"✅ Thinking level set to: {thinking_level}")
                elif user_input:
                    history.append({"role": "user", "content": user_input})
                    asyncio.run(parallel_process(user_input, history))
            except (KeyboardInterrupt, EOFError):
                break
    elif args.prompt:
        asyncio.run(parallel_process(args.prompt, []))
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
