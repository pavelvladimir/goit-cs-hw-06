"""Serve the export with production routes/CSP for isolated browser tests."""

from main import WebServer

if __name__ == "__main__":
    # Browser unit tests intercept POSTs. Real writes use the Compose integration suite.
    with WebServer(("127.0.0.1", 4176), ("127.0.0.1", 1)) as server:
        print("Preview: http://127.0.0.1:4176", flush=True)
        server.serve_forever()
