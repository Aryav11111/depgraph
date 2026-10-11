"""Print Makefile targets and their `## description` comments (used by `make help`)."""

import re
from pathlib import Path

makefile = Path(__file__).resolve().parent.parent / "Makefile"
for target, desc in re.findall(r"^([a-z]+):.*?## (.*)$", makefile.read_text(), re.M):
    print(f"{target:<10} {desc}")
