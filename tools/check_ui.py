"""Check the web UI's inline script for unterminated string literals.

A quoted string cannot contain a raw newline in JavaScript. When one slips in,
the parser rejects the whole <script> block and the page silently does nothing:
no camera list, every badge stuck on its placeholder. That is easy to introduce
with an editing tool that mangles backslash escapes, and easy to miss, because
the page still loads and looks structurally fine.

This walks the script character by character, tracking whether it is inside a
string, a template literal, a regex or a comment, and reports any quoted string
that runs off the end of its line.

Run it after editing app/controller/templates/index.html:

    python tools/check_ui.py
"""
import os
import sys

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "controller", "templates", "index.html",
)


def scan(path=DEFAULT_PATH):
    """Return [(line_in_file, message)] for every unterminated string."""
    source = open(path, encoding="utf-8").read()
    start = source.rindex("<script>") + len("<script>")
    end = source.rindex("</script>")
    return _walk(source[start:end], source[:start].count("\n") + 1)


def _walk(script, line_offset):
    problems = []
    i = 0
    line = 1
    state = None          # None | "'" | '"' | "`" | "//" | "/*"
    state_line = 0

    while i < len(script):
        ch = script[i]
        nxt = script[i + 1] if i + 1 < len(script) else ""

        if ch == "\n":
            line += 1
            if state in ("'", '"'):
                problems.append((state_line + line_offset - 1, f"string opened with {state} never closed on its line"))
                state = None
            elif state == "//":
                state = None
            i += 1
            continue

        if state in ("'", '"', "`"):
            if ch == "\\":
                i += 2
                continue
            if ch == state:
                state = None
            i += 1
            continue

        if state == "//":
            i += 1
            continue

        if state == "/*":
            if ch == "*" and nxt == "/":
                state = None
                i += 2
                continue
            i += 1
            continue

        # Not inside anything yet.
        if ch == "/" and nxt == "/":
            state = "//"
            i += 2
            continue
        if ch == "/" and nxt == "*":
            state = "/*"
            i += 2
            continue
        if ch == "/":
            # A slash here is either division or a regex literal. Only a regex can
            # follow these, and a regex may legitimately contain a bare quote.
            previous = script[:i].rstrip()
            if not previous or previous[-1] in "(,=:[!&|?{};+" or previous.endswith("return"):
                j = i + 1
                while j < len(script) and script[j] not in ("\n",):
                    if script[j] == "\\":
                        j += 2
                        continue
                    if script[j] == "/":
                        break
                    j += 1
                if j < len(script) and script[j] == "/":
                    i = j + 1
                    continue
            i += 1
            continue
        if ch in "'\"`":
            state = ch
            state_line = line
            i += 1
            continue
        i += 1

    if state in ("'", '"', "`"):
        problems.append((state_line + line_offset - 1, f"string opened with {state} never closed"))
    return problems


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    problems = scan(path)
    lines = open(path, encoding="utf-8").read().splitlines()
    for absolute, message in problems:
        print(f"FAIL index.html:{absolute}  {message}")
        print(f"     {lines[absolute - 1].strip()[:110]}")
    if problems:
        print(f"{len(problems)} unterminated string(s)")
        return 1
    print("PASS no unterminated string literals in the inline script")
    return 0


if __name__ == "__main__":
    sys.exit(main())
