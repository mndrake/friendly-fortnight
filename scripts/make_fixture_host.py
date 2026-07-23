#!/usr/bin/env python3
"""Dump the synthetic test estate as a fixture-host directory.

The resulting directory can be served to the CLI with
``--fixture-dir`` so the whole pipeline runs with no IBM i:

    python scripts/make_fixture_host.py data/fixture-host
    python cli.py run --config config.fixture.yaml --fixture-dir data/fixture-host
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "data/fixture-host")
    out.mkdir(parents=True, exist_ok=True)
    from conftest import build_session
    session = build_session()
    for tag, result in session._responses.items():  # noqa: SLF001
        (out / f"{tag}.json").write_text(
            json.dumps({"columns": result.columns,
                        "rows": [list(r) for r in result.rows]}, indent=1),
            encoding="utf-8")
    print(f"wrote {len(session._responses)} fixture responses to {out}")


if __name__ == "__main__":
    main()
