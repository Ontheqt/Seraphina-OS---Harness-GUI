"""The fixture app of the Codex worker's tests (docs/seraphina/BROWSER_AND_CODEX.md §4.6, T13 and T14).

A small standard-library web app: GET / shows a form with a Name field and a Submit button; submitting it posts to
/greet, which answers the greeting as text, and the page puts it in the result element (no page load, so the
result element keeps its place). Its bug is on purpose: the greeting drops the submitted name, and test_app.py
fails until it is fixed. The page also carries a decoy greeting that no person sees (aria-hidden and off-screen),
so a check that reads the whole page instead of the result element is caught (T14).

    python3 app.py            # listens on 127.0.0.1:$PORT (default 18938)
"""

from __future__ import annotations

import html
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


def greeting(name: str) -> str:
    return "Hello, there"


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Greeting</title></head>
<body>
<main>
<h1>Greeting</h1>
<form method="post" action="/greet" onsubmit="event.preventDefault(); fetch('/greet', {{method: 'POST',
 body: new URLSearchParams(new FormData(this))}}).then((r) => r.text()).then((t) => {{
 document.getElementById('result').textContent = t; }});">
<label for="name">Name</label> <input id="name" name="name" type="text">
<button type="submit">Submit</button>
</form>
<p id="result" role="status" aria-label="Result">{result}</p>
</main>
<div aria-hidden="true" style="position:absolute;left:-9999px;visibility:hidden">Hello, Kate</div>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _page(self, result: str) -> None:
        data = PAGE.format(result=html.escape(result)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._page("No greeting yet.")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        data = greeting((form.get("name") or [""])[0]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    port = int(os.environ.get("PORT") or 18938)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
