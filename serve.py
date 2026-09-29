"""v0.21.0: production entry point (used by the Docker image).

    python serve.py        # ORCH_HOST / ORCH_PORT, default 127.0.0.1:5050

Runs the one-time JSON -> SQLite migration, then serves the UI with
waitress (a production WSGI server) when installed, else Flask's server.
"""

import os
import sys


def main():
    import orch_ui

    orch_ui.startup()
    host = os.getenv("ORCH_HOST") or "127.0.0.1"
    port = int(os.getenv("ORCH_PORT") or 5050)
    print(f"ORCH {orch_ui.APP_VERSION} on http://{host}:{port} "
          f"(secret key: {orch_ui.SECRET_KEY_SOURCE})", file=sys.stderr, flush=True)
    try:
        from waitress import serve
    except ImportError:
        orch_ui.app.run(host=host, port=port, debug=False)
        return 0
    serve(orch_ui.app, host=host, port=port,
          threads=int(os.getenv("ORCH_THREADS") or 8), ident="ORCH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
