"""Run the API and UI.

python scripts/serve.py            # http://127.0.0.1:8080
python scripts/serve.py --port 9000 --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn

    print(f"\n  UI  http://{args.host}:{args.port}/")
    print(f"  API http://{args.host}:{args.port}/docs\n")
    uvicorn.run(
        "bilingual_rag.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
