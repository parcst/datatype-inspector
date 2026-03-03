"""Entry point for ``python -m datatype_inspector``."""

import uvicorn


def main() -> None:
    uvicorn.run(
        "datatype_inspector.app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )


if __name__ == "__main__":
    main()
