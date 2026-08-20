"""Writes the API's OpenAPI document to frontend/openapi.json.

The frontend's TypeScript types are generated from this file
(`npm run gen:api` in frontend/), so the React app's types cannot drift
from the backend's Pydantic response models - a renamed or retyped field
breaks the frontend build instead of surfacing as `undefined` at runtime
in a browser.

Run this after changing anything in api/schemas/.
"""

import json
from pathlib import Path

OUTPUT_PATH = Path("frontend/openapi.json")


def main() -> None:
    from api.main import app

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(app.openapi(), indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
