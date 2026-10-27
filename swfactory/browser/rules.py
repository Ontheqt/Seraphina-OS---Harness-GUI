"""The browser's rules as pure functions (BROWSER_AND_CODEX.md §2.3 to §2.5): URL and origin rules, the parsing of
Playwright MCP 0.0.83's responses (page, snapshot, tabs, modal state, events, the request list), action classes
from the role and from what the browser sent, and the page text a model sees. Standard library only; the shim
imports this module inside its namespace."""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field

# ---------------------------------------------------------------- URL and origin rules (§2.4)

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}
BLANK = "about:blank"
URL_CHARS = 300          # caps on page-derived strings (§2.3, proposed)
TITLE_CHARS = 200
DIALOG_CHARS = 300
FILE_NAME_CHARS = 120
NAME_CHARS = 200


class UrlRefused(ValueError):
    """A URL that failed the parsing rules; reason is one of scheme, userinfo, unparseable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ParsedUrl:
    origin: str           # scheme://host:port, normalized; "about:blank" for the empty page
    path: str             # path and query (what §2.3's stale-ref rule compares), never written to a record
    page: str             # the origin and path without query and fragment (the duplicate-commit key, §2.5)


def _host(raw: str) -> str:
    host = raw.strip().rstrip(".").lower()
    if not host:
        raise UrlRefused("unparseable")
    if host.startswith("["):   # an IPv6 literal keeps its brackets
        return host
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UrlRefused("unparseable") from exc


def parse_url(url: object) -> ParsedUrl:
    """url parsed by the rules of §2.4: http and https only (plus about:blank), no userinfo, the host normalized
    (IDNA to ASCII, lowercase, no trailing dot) and the port made explicit. UrlRefused otherwise."""
    if not isinstance(url, str) or not url.strip() or len(url) > 4096 or any(ord(ch) < 32 for ch in url):
        raise UrlRefused("unparseable")
    text = url.strip()
    if text == BLANK:
        return ParsedUrl(BLANK, "", BLANK)
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise UrlRefused("unparseable") from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlRefused("scheme")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise UrlRefused("userinfo")
    hostname = parts.hostname
    if not hostname:
        raise UrlRefused("unparseable")
    host = _host(f"[{hostname}]" if ":" in hostname else hostname)
    origin = f"{scheme}://{host}:{port if port is not None else DEFAULT_PORTS[scheme]}"
    path = parts.path or "/"
    return ParsedUrl(origin, path + (f"?{parts.query}" if parts.query else ""), origin + path)


def normalize_origin(origin: object) -> str:
    """An origin as a grant holds it: parsed like a URL, with nothing after the host and port. ValueError when it
    is not an http or https origin."""
    try:
        parsed = parse_url(origin)
    except UrlRefused as exc:
        raise ValueError(f"not an http or https origin: {origin!r} ({exc.reason})") from None
    rest = urllib.parse.urlsplit(str(origin).strip())
    if parsed.origin == BLANK or rest.path not in ("", "/") or rest.query or rest.fragment:
        raise ValueError(f"an origin has no path, query or fragment: {origin!r}")
    return parsed.origin


def loopback_origin(origin: str) -> bool:
    """Whether an origin is on the loopback inside the namespace (BC1 accepts no other, §2.7)."""
    host = urllib.parse.urlsplit(origin).hostname or ""
    return host == "localhost" or host.startswith("127.") or host == "::1"


# ---------------------------------------------------------------- Playwright MCP's responses (0.0.83)

_SECTION_RE = re.compile(r"^### (.+)$", re.MULTILINE)
_TAB_RE = re.compile(r"^- (\d+): (\(current\) )?\[(.*)\]\((.*)\)$")
# A dialog's line in the Modal state section. Playwright writes the page's message into it unescaped, so it may hold
# newlines and lines that look like sections: the type comes from the prefix, which the page cannot write, and the
# message runs to the last end marker of the response (cutting too much only loses parsing, never forges it).
_DIALOG_START_RE = re.compile(r'^### Modal state\n- \["(alert|confirm|prompt|beforeunload)" dialog with message "',
                              re.MULTILINE)
_DIALOG_END = '"]: can be handled by '
MODAL_ERROR = "does not handle the modal state"   # 0.0.83's words for a call a dialog or file chooser blocked
_FILE_CHOOSER_RE = re.compile(r"^- \[File chooser\]")
_DOWNLOADED_RE = re.compile(r'^- Downloaded file (.+) to "(.*)"$')
_REQUEST_RE = re.compile(r"^(\d+)\. \[([A-Za-z]+)\] (\S+)(?: => \[([^\]]*)\](?: (.*))?)?$")
_LINE_RE = re.compile(r"^(?P<indent> *)- (?P<body>.*)$")
_NAME_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_ATTR_RE = re.compile(r"\[([a-z-]+)(?:=([^\]]*))?\]")
REF_RE = re.compile(r"^(?:f\d+)?e\d+$")


@dataclass
class Node:
    """One line of an ARIA snapshot that has a ref."""

    ref: str
    role: str
    name: str
    text: str             # the inline text after the colon, unquoted
    depth: int
    index: int            # the line's position in the snapshot
    attrs: dict = field(default_factory=dict)
    url: str | None = None   # a link's /url property


@dataclass
class Page:
    """What one Playwright response says about the page. Everything but the counts is page-controlled."""

    url: str | None = None
    title: str | None = None
    snapshot: list[str] = field(default_factory=list)   # the YAML lines, as Playwright gave them
    nodes: dict[str, Node] = field(default_factory=dict)
    tabs: list[dict] = field(default_factory=list)       # [{index, current, title, url}]
    modal: str = "none"                                  # none, alert, confirm, prompt, file_chooser
    dialog_text: str | None = None
    downloads: list[dict] = field(default_factory=list)  # [{name, path}]
    error: str | None = None
    result: str | None = None
    has_snapshot: bool = False


def sections(text: str) -> dict[str, str]:
    """{heading: body} of a response's ### sections (the last one of a heading wins)."""
    found: dict[str, str] = {}
    marks = list(_SECTION_RE.finditer(text or ""))
    for number, mark in enumerate(marks):
        end = marks[number + 1].start() if number + 1 < len(marks) else len(text)
        found[mark.group(1).strip()] = text[mark.end():end].strip("\n")
    return found


def _yaml_block(body: str) -> list[str] | None:
    match = re.search(r"```yaml\n(.*?)```", body or "", re.DOTALL)
    if match is None:
        return None
    return [line for line in match.group(1).split("\n") if line.strip()]


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            return str(json.loads(text))
        except ValueError:
            return text[1:-1]
    return text


def parse_snapshot(lines: list[str]) -> dict[str, Node]:
    """{ref: Node} of an ARIA snapshot's lines (role, accessible name, inline text, depth, attributes, a link's
    /url). Lines without a ref (text, properties) are kept in the snapshot but are not targets."""
    nodes: dict[str, Node] = {}
    last: Node | None = None
    for index, line in enumerate(lines):
        match = _LINE_RE.match(line)
        if match is None:
            continue
        depth = len(match.group("indent")) // 2
        body = match.group("body")
        if body.startswith("/url:") and last is not None and depth == last.depth + 1:
            last.url = _unquote(body[len("/url:"):])
            continue
        role = re.match(r"[^\s:\[\"]+", body)
        if role is None:
            continue
        rest = body[role.end():]
        name = ""
        quoted = _NAME_RE.match(rest.lstrip())
        if quoted is not None:
            name = _unquote(quoted.group(0))
            rest = rest.lstrip()[quoted.end():]
        attrs: dict = {}
        for attr in _ATTR_RE.finditer(rest.split(":", 1)[0] if '"' not in rest.split(":", 1)[0] else rest):
            attrs[attr.group(1)] = attr.group(2) if attr.group(2) is not None else True
        text = ""
        tail = _ATTR_RE.sub("", rest).strip()
        if tail.startswith(":"):
            text = _unquote(tail[1:])
        ref = attrs.get("ref")
        if isinstance(ref, str) and REF_RE.match(ref):
            node = Node(ref, role.group(0), name, text, depth, index, {k: v for k, v in attrs.items() if k != "ref"})
            nodes[ref] = node
            last = node
        else:
            last = None
    return nodes


def _cut_dialog(text: str) -> tuple[str, str | None, str | None]:
    """(text with a dialog's line replaced by a placeholder, the dialog's type, its message), or (text, None, None).
    Found before the text is split into sections, so a message holding newlines or "### " lines is one dialog and
    never a section of its own."""
    start = _DIALOG_START_RE.search(text or "")
    if start is None:
        return text, None, None
    end = text.rfind(_DIALOG_END)
    if end < start.end():   # no end marker after it: the rest of the response is the message
        message, rest = text[start.end():], ""
    else:
        line_end = text.find("\n", end)
        message, rest = text[start.end():end], (text[line_end:] if line_end >= 0 else "")
    kind = "confirm" if start.group(1) == "beforeunload" else start.group(1)
    return text[:start.start()] + "### Modal state\n- [dialog]" + rest, kind, message


def parse_response(text: str) -> Page:
    """A Playwright MCP tool response read into a Page."""
    page = Page()
    text, dialog, message = _cut_dialog(text or "")
    if dialog is not None:
        page.modal, page.dialog_text = dialog, message
    found = sections(text)
    for line in (found.get("Page") or "").split("\n"):
        if line.startswith("- Page URL: "):
            page.url = line[len("- Page URL: "):].strip()
        elif line.startswith("- Page Title: "):
            page.title = line[len("- Page Title: "):]
    lines = _yaml_block(found.get("Snapshot") or "")
    if lines is not None:
        page.snapshot = lines
        page.nodes = parse_snapshot(lines)
        page.has_snapshot = True
    for line in (found.get("Open tabs") or "").split("\n"):
        match = _TAB_RE.match(line.strip())
        if match:
            page.tabs.append({"index": int(match.group(1)), "current": bool(match.group(2)),
                              "title": match.group(3), "url": match.group(4)})
    if "Result" in found and found["Result"].lstrip().startswith("- 0:"):   # browser_tabs answers in a Result
        for line in found["Result"].split("\n"):
            match = _TAB_RE.match(line.strip())
            if match:
                page.tabs.append({"index": int(match.group(1)), "current": bool(match.group(2)),
                                  "title": match.group(3), "url": match.group(4)})
    for line in (found.get("Modal state") or "").split("\n"):
        if dialog is None and _FILE_CHOOSER_RE.match(line.strip()):
            page.modal = "file_chooser"
    for line in (found.get("Events") or "").split("\n"):
        match = _DOWNLOADED_RE.match(line.strip())
        if match:
            page.downloads.append({"name": match.group(1), "path": match.group(2)})
    if "Error" in found:
        page.error = found["Error"].strip()
    if "Result" in found:
        page.result = found["Result"].strip()
    return page


def requests_of(text: str) -> list[dict]:
    """browser_network_requests' list: [{n, method, url, status, line}] (status None while no answer came)."""
    out = []
    for line in (sections(text).get("Result") or text or "").split("\n"):
        match = _REQUEST_RE.match(line.strip())
        if match:
            out.append({"n": int(match.group(1)), "method": match.group(2).upper(), "url": match.group(3),
                        "status": match.group(4), "line": line.strip()})
    return out


def new_requests(before: list[dict], after: list[dict]) -> list[dict]:
    """The requests in after that were not in before. Playwright numbers them per page load, so when before is not
    a prefix of after (a navigation started a new list) every request in after is new."""
    lines = [entry["line"] for entry in before]
    if [entry["line"] for entry in after[:len(lines)]] == lines:
        return after[len(lines):]
    return list(after)


# ---------------------------------------------------------------- action classes (§2.5)

LINK_ROLES = frozenset({"link"})
EDITABLE_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})
FILL_TYPES = {"textbox": "textbox", "searchbox": "textbox", "spinbutton": "textbox", "combobox": "combobox",
              "checkbox": "checkbox", "radio": "radio", "slider": "slider"}
NAMED_KEYS = frozenset({"Enter", "Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Backspace",
                        "Delete"})
SUBMITTING_KEYS = frozenset({"Enter", " "})   # judgement: Enter submits a form, Space activates a focused button
QUIET_METHODS = frozenset({"GET", "HEAD"})


def valid_key(key: object) -> bool:
    """Enter, Tab, Escape, the four arrows, Backspace, Delete, or one printable character; no modifiers."""
    return isinstance(key, str) and (key in NAMED_KEYS or (len(key) == 1 and key.isprintable()))


def click_class(role: str) -> str:
    """A link is navigate (then checked by its effect, K15); any other role, known or not, is a commit."""
    return "navigate" if role in LINK_ROLES else "commit"


def press_class(key: str) -> str:
    """Enter (or Space) is a commit whatever has the focus: form membership cannot be read reliably (§2.5)."""
    return "commit" if key in SUBMITTING_KEYS else "input"


def sent_something(requests: list[dict]) -> bool:
    """Whether the browser sent anything but a body-less GET or HEAD. 0.0.83's list gives the method and the
    status, not whether a request had a body (pinned by the recorded responses), so any other method counts."""
    return any(entry["method"] not in QUIET_METHODS for entry in requests)


# ---------------------------------------------------------------- the page text a model sees (§2.3)


def subtree(lines: list[str], node: Node) -> list[str]:
    """The node's line and every line below it that is indented deeper."""
    out = [lines[node.index]]
    for line in lines[node.index + 1:]:
        match = _LINE_RE.match(line)
        if match is not None and len(match.group("indent")) // 2 <= node.depth:
            break
        out.append(line)
    return out


def element_text(lines: list[str], node: Node) -> str:
    """One element's text: its name, its inline text and the texts below it, joined by spaces."""
    parts = [node.name, node.text] if node.name != node.text else [node.name]
    for line in subtree(lines, node)[1:]:
        match = _LINE_RE.match(line)
        if match is None:
            continue
        body = match.group("body")
        if body.startswith("/"):
            continue
        child = parse_snapshot([body if body.startswith("-") else f"- {body}"])
        if child:
            item = next(iter(child.values()))
            parts += [item.name, item.text]
        elif ":" in body:
            parts.append(_unquote(body.split(":", 1)[1]))
        else:
            name = _NAME_RE.search(body)
            parts.append(_unquote(name.group(0)) if name else "")
    return " ".join(part for part in (" ".join(str(p).split()) for p in parts) if part)


def depth_cut(lines: list[str], depth: int) -> list[str]:
    """The lines at most depth levels deep (the top of the tree)."""
    out = []
    for line in lines:
        match = _LINE_RE.match(line)
        if match is None or len(match.group("indent")) // 2 < depth:
            out.append(line)
    return out


def find_lines(lines: list[str], needle: str) -> list[str]:
    """The lines whose text contains needle (a literal, case-insensitive) with each one's ancestors, in order."""
    wanted = needle.casefold()
    keep: set[int] = set()
    stack: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = _LINE_RE.match(line)
        depth = len(match.group("indent")) // 2 if match else 0
        while stack and stack[-1][1] >= depth:
            stack.pop()
        if wanted in line.casefold():
            keep.add(index)
            keep.update(position for position, _ in stack)
        stack.append((index, depth))
    return [line for index, line in enumerate(lines) if index in keep]
