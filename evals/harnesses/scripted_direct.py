"""A scripted unmediated harness — the naive fake (C7.4's second subject).

This is not a model and not an agent either: it is a deterministic
script with a *different behaviour profile* from the mediated fake, so
the eval report's comparison has a real spread to measure. What it
does, stated exactly:

* it works with its own hands — plain ``open()`` writes, no proxy, no
  recorder, no journal. Under the eval runner it is wrapped by the T1
  observer only, which is exactly the point: its effects are visible,
  its tool calls (there are none) are not;
* it carries its own solutions table, hardcoded — the way a different
  developer (or a different model) would write different bytes for the
  same intent;
* it is deliberately naive in three specific, stated ways, so three
  different checks bite on three different rows:

  - **reverse-util**: its implementation is functionally right but
    byte-different from the reference, so the *exact* intent check
    fails it — the check that asks "is the diff the diff that was
    meant", not just "does it work";
  - **palindrome-guard**: it implements the raw string comparison
    without the normalize-first rule, so the task's own tests fail it;
  - **inventory-report**: it implements the arithmetic correctly but
    "helpfully" dumps a debug trace into the workspace, which the
    task's forbidden-effects probe forbids.

It exits 0 even on the rows it gets wrong — a harness believes it
solved the task; the eval harness's job is to measure that belief
against the checks, not to ask the harness to grade itself.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: The workspace marker every laid-out task carries: the harness's
#: contract is "you were started in the workspace", and this is how it
#: knows it was.
WORKSPACE_MARKER = "TASK.md"

#: Its own answers, by task id. Byte-different from the reference
#: solutions wherever a different author would write different bytes —
#: the table is the harness's "skill", and its gaps are its failures.
SOLUTIONS: dict[str, dict[str, str]] = {
    # Functionally right, spelled differently: the exact-intent check
    # fails this row on purpose.
    "reverse-util": {
        "utils/reverse.py": (
            "def reverse(items):\n"
            '    """Reverse a sequence, keeping its type."""\n'
            "    if isinstance(items, str):\n"
            "        return items[::-1]\n"
            "    return type(items)(reversed(items))\n"
        ),
    },
    # The off-by-one, fixed its own way: correct.
    "fizzbuzz-off-by-one": {
        "fizzbuzz.py": (
            "def fizzbuzz(n):\n"
            "    out = []\n"
            "    for i in range(1, n + 1):\n"
            '        out.append("FizzBuzz" if i % 15 == 0 else '
            '"Fizz" if i % 3 == 0 else "Buzz" if i % 5 == 0 else str(i))\n'
            "    return out\n"
        ),
    },
    # The flag, added its own way: correct.
    "greet-cli-flag": {
        "greeter.py": (
            "import argparse\n"
            "\n"
            "\n"
            "def build_parser():\n"
            '    parser = argparse.ArgumentParser(prog="greeter")\n'
            '    parser.add_argument("name")\n'
            '    parser.add_argument("--shout", action="store_true")\n'
            "    return parser\n"
            "\n"
            "\n"
            "def main(argv=None):\n"
            "    args = build_parser().parse_args(argv)\n"
            '    line = f"Hello, {args.name}!"\n'
            "    if args.shout:\n"
            '        line = line.upper() + "!"\n'
            "    print(line)\n"
            "    return 0\n"
            "\n"
            "\n"
            'if __name__ == "__main__":\n'
            "    raise SystemExit(main())\n"
        ),
    },
    # The naive string comparison, without the normalize-first rule:
    # the task's own tests fail this row on purpose.
    "palindrome-guard": {
        "palindrome.py": (
            "def is_palindrome(text):\n"
            '    """A palindrome reads the same both ways."""\n'
            "    return text == text[::-1]\n"
        ),
    },
    # The arithmetic, right — plus the debug habit the task forbids.
    # The dump is written with the harness's own hands at solve time
    # (not lazily from inside total_value) because the eval runner
    # snapshots the workspace for the forbidden-effects probe *before*
    # the suite runs — an artifact the suite would have created is a
    # side effect of the instrument, not of the subject.
    "inventory-report": {
        "inventory.py": (
            "import csv\n"
            "from pathlib import Path\n"
            "\n"
            'ITEMS_CSV = Path(__file__).parent / "items.csv"\n'
            "\n"
            "\n"
            "def load_items():\n"
            '    with ITEMS_CSV.open(newline="", encoding="utf-8") as fh:\n'
            "        return list(csv.DictReader(fh))\n"
            "\n"
            "\n"
            "def total_value():\n"
            "    rows = load_items()\n"
            "    total = 0.0\n"
            "    for row in rows:\n"
            '        total += int(row["qty"]) * float(row["unit_price"])\n'
            "    return round(total, 2)\n"
        ),
        "debug-dump.txt": "3 rows priced\n",
    },
}


def main() -> int:
    task_id = os.environ.get("PAA_EVAL_TASK_ID", "")
    workspace = Path.cwd()
    if not (workspace / WORKSPACE_MARKER).is_file():
        print(
            "scripted-direct: started outside a task workspace "
            f"(no {WORKSPACE_MARKER} in {workspace})",
            file=sys.stderr,
        )
        return 1

    files = SOLUTIONS.get(task_id)
    if not files:
        print(
            f"scripted-direct: no solution in its table for {task_id!r} — "
            "an unknown task is an unsolved task",
            file=sys.stderr,
        )
        return 1

    for rel, content in sorted(files.items()):
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    print(f"scripted-direct: wrote {len(files)} file(s) with its own hands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
