"""Turn a pytest JUnit report into GitHub annotations.

Job logs on GitHub are only readable when signed in; annotations are public.
Without this, a failing run says "exit code 1" and nothing else to anyone
diagnosing it from outside.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def main(path: str) -> int:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"::warning::no JUnit report to summarise ({exc})")
        return 0
    failed = 0
    for case in root.iter("testcase"):
        for problem in (*case.findall("failure"), *case.findall("error")):
            failed += 1
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            detail = (problem.get("message") or problem.text or "").strip()
            # One line per annotation; GitHub keeps the first ~4 KB.
            detail = " | ".join(line for line in detail.splitlines() if line.strip())[:1500]
            path_hint = case.get("file") or case.get("classname", "").replace(".", "/") + ".py"
            print(f"::error file={path_hint},title={name}::{detail or 'failed'}")
    print(f"{failed} failing test(s) reported")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "junit.xml"))
