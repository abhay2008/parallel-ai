import sys
import select

def get_input(prompt):
    print(prompt, end='', flush=True)
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

if __name__ == "__main__":
    print("Paste multiline input (press Enter):")
    try:
        inp = get_input("You: ")
        print("Captured:", repr(inp))
    except (EOFError, KeyboardInterrupt):
        print("\nExiting.")
