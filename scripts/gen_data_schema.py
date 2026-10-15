"""Write the JSON Schema of the data profile to src/awa/data/profile.v1.schema.json.

Run it from the repository root after changing src/awa/data/_model.py:

    python scripts/gen_data_schema.py            # rewrite the file
    python scripts/gen_data_schema.py --check    # exit 1 when the file is out of date

tests/unit/test_data_model.py fails while the checked-in file differs from the model.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from awa.data import DataProfile

TARGET = Path(__file__).resolve().parents[1] / "src" / "awa" / "data" / "profile.v1.schema.json"


def main(argv: list[str]) -> int:
    text = json.dumps(DataProfile.json_schema(), indent=2, ensure_ascii=False) + "\n"
    if "--check" in argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
        if current != text:
            print(f"{TARGET} is out of date; run python scripts/gen_data_schema.py")
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
