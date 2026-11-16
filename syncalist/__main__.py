"""Run with:  python -m syncalist"""

import os

import uvicorn


def main() -> None:
    uvicorn.run("syncalist.web.app:create_app", factory=True, host="0.0.0.0",
                port=int(os.environ.get("PORT", "8585")), proxy_headers=True)


if __name__ == "__main__":
    main()
