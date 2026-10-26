"""Per-area mock backends for tests/gui_mock_server.py (one module per GUI area, so areas never edit one file).

gui_mock_server loads the modules named in AREAS, in this order, and calls these optional functions:

    seed(core, fresh: bool) -> None
        After the core seed (also with --fresh, and on every mock.seed() of a test). Keep the area's data in
        store(core, AREA), a dict that seed resets: core.STATE["area"][AREA].
    api(core, handler, method: str, parts: list[str], query: dict, body) -> Any
        Every /api/... request, BEFORE the core's own routes. Answer with handler.send_json(obj, status=200) or
        handler.error(status, msg) (or raise core.Invalid / core.Conflict / KeyError for 400 / 409 / 404) and return
        anything but NotImplemented; return NotImplemented to pass the request on (the next area, then the core).
        parts: the path after /api/ split on "/", unquoted; query: {name: last value}; body: the JSON body (POST).
        An area may answer a core route to add its fields (say POST /api/jobs with a profile): it then owns it.
    mock(core, handler, method: str, name: str, body) -> Any
        Test hooks at /__mock/<area>/<name>; NotImplemented when the name is not the area's.
    contract(test) -> None
        Checks of the area's shapes against swfactory/, run by tests/test_gui_static.py
        (MockServerContractTest.test_area_contracts): test.request(method, path, body=None) -> (status,
        content_type, raw bytes); test.assert*/subTest as in unittest.

core is the gui_mock_server module itself (STATE, LOCK, BUS, now(), ago(), iso(), Invalid, Conflict, new_job,
save_job, public_job, ...). Never import gui_mock_server from an area module: run as a script it is __main__, and
an import would load a second copy with its own state. Publish events with core.BUS.publish(type, data, repo=None,
job=None, agent=None). Seed hostile text (markup, javascript: links) wherever the GUI must show raw text.
"""

from __future__ import annotations

import importlib

AREAS = ("routing", "tracker", "personas", "messages", "operator", "backups")


def load() -> list:
    """The area modules, in AREAS order."""
    return [importlib.import_module(f"{__name__}.{name}") for name in AREAS]


def store(core, area: str) -> dict:
    """The area's own part of the mock state (reset by every seed)."""
    return core.STATE.setdefault("area", {}).setdefault(area, {})
