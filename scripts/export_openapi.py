"""Export/check the dependency-free API contract declared by main.py."""

import argparse
import json
from pathlib import Path

from main import openapi_schema

TARGET = Path(__file__).resolve().parents[1] / "frontend" / "openapi.json"


def export(target: Path = TARGET, *, check: bool = False) -> None:
    content = json.dumps(openapi_schema(), ensure_ascii=False, indent=2) + "\n"
    if check:
        if not target.is_file() or target.read_text() != content:
            raise SystemExit("OpenAPI snapshot is stale. Run make generate-api.")
    else:
        target.write_text(content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    export(check=parser.parse_args().check)
