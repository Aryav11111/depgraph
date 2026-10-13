"""Write the API's OpenAPI schema to web/openapi.json (no server, no database).

The frontend's TypeScript types are generated from this file, so it is committed:
the web app builds without a running backend, and CI fails if the API and the
committed schema drift apart (`make openapi` then `git diff --exit-code`).
"""

import json
import sys
from pathlib import Path

from depgraph.core.config import Settings
from depgraph.main import create_app

OUT = Path(__file__).resolve().parent.parent / "web" / "openapi.json"


def main() -> None:
    schema = create_app(Settings(log_json=False)).openapi()
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            sys.exit("web/openapi.json is stale: run `make openapi` and commit the result")
        return
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(OUT.parent.parent)} ({len(schema['paths'])} paths)")


if __name__ == "__main__":
    main()
