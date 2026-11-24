"""WSGI entry point for the exposure ledger."""

import os

from ledger.app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("LEDGER_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEDGER_PORT", "8000")),
        debug=os.environ.get("LEDGER_DEBUG") == "1",
    )
