# The fixture app

The Codex worker's tests (docs/seraphina/BROWSER_AND_CODEX.md §4.6, T13 to T16, T19 and T14) copy this folder into a
fresh git repository. Its greeting drops the submitted name on purpose, so `python3 -m unittest -q` fails until a
builder fixes `greeting()` in app.py. `python3 app.py` serves it on 127.0.0.1:$PORT for "Verify in the browser".
The page also carries a hidden decoy greeting: a check that reads the whole page instead of the result element
reads it, and T14's variant catches that.
