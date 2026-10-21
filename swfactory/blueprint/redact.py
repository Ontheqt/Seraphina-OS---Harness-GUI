"""Redaction for Blueprint captures (DESIGN.md §28, Capture format v0).

Everything a probe prints passes through one Redactor before it is stored, and the Redactor counts
what it removed per category for the capture's redaction review.

Text rules, in order: private-key blocks; JWT-like strings; the userinfo of credential URLs
(scheme://user:pass@host keeps scheme and host); Bearer/Basic/Token header values; `name: value`
(a space after the colon, so host:port survives) and `name=value` pairs, and `--name value`
arguments (also as neighbouring items of a flow list, `["--name", "value"]`), whose name contains
KEY, TOKEN, SECRET, PASS or AUTH in any case (a colon pair loses the rest of its line, an equals
pair its next word or quoted string); then every value the caller already knows is secret (the
factory's own credential-named environment).

JSON is redacted by structure: every value under a secret-looking key (booleans and null kept, so
"AuthEnabled": false still says something), every container Env value (`NAME=value` →
`NAME=[REDACTED]`, whatever the name), the argument after a secret-looking `--flag` in an argv
list, and every other string by the text rules.

YAML (netplan get, compose files) is redacted by indentation, a line scan with no parser: a key
with a secret word (one of its words, split at -, _, . and camelCase, ends in key, token, secret,
password, passwd, passphrase, auth, credential or psk, or is pass) loses every value in its
indented block (`keys:` → `private: [REDACTED]`) and every line of its block scalar
(`password: |`); the list item after a secret-looking `- --flag` goes; the text rules run on every
line. A whole word, not a substring, so a service called authelia or keycloak keeps its block.
Compose files also lose every value of an `environment:` block, whatever its name, inline or as a
list or map.

Pattern matching is not proof: the capture's review says so, and the owner reads a capture before
sharing it.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

MARK = "[REDACTED]"
SECRET_WORD_RE = re.compile(r"KEY|TOKEN|SECRET|PASS|AUTH", re.IGNORECASE)
NAME_LIKE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
SECRET_FLAG_RE = re.compile(r"^-{1,2}[A-Za-z0-9_.-]*(?:key|token|secret|pass|auth)[A-Za-z0-9_.-]*$", re.IGNORECASE)
_NAME_BODY = r"[A-Za-z0-9_.-]*(?:KEY|TOKEN|SECRET|PASS|AUTH)[A-Za-z0-9_.-]*"
_NAME = r"(?<![A-Za-z0-9_.-])(" + _NAME_BODY + ")"
_NOT_DONE = r"(?![\"']?\[REDACTED\])"

PRIVATE_KEY_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]*)PRIVATE KEY-----.*?(?:-----END \1PRIVATE KEY-----|\Z)", re.DOTALL)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")
URL_USERINFO_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://)(?!\[REDACTED\]@)([^/\s@\"'<>]+)@")
AUTH_HEADER_RE = re.compile(r"\b(Bearer|Basic|Token)\s+" + _NOT_DONE + r"[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
QUOTED_COLON_RE = re.compile(r"(\"" + _NAME_BODY + r"\"\s*:\s*)" + _NOT_DONE + r"(\"(?:[^\"\\\n]|\\.)*\"|[^\s,}\]]+)",
                             re.IGNORECASE)
COLON_PAIR_RE = re.compile(_NAME + r"([\"']?)(\s*:[ \t]+)" + _NOT_DONE
                           + r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s#][^\n]*?)(?=[ \t]+#|[ \t]*$)",
                           re.IGNORECASE | re.MULTILINE)
EQUALS_PAIR_RE = re.compile(_NAME + r"([\"']?)(\s*=\s*)" + _NOT_DONE + r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"'&;,]+)",
                            re.IGNORECASE)
FLAG_ARGUMENT_RE = re.compile(r"(?<!\S)(--?" + _NAME_BODY + r")([ \t]+)" + _NOT_DONE + r"(?!-)(\S+)", re.IGNORECASE)
FLAG_LIST_RE = re.compile(r"(?<![A-Za-z0-9_.-])([\"']?)(--?" + _NAME_BODY + r")\1([ \t]*,[ \t]*)" + _NOT_DONE
                          + r"(?![\"']?-)(\"[^\"\n]*\"|'[^'\n]*'|[^\s,\]\"']+)", re.IGNORECASE)
ENV_BLOCK_RE = re.compile(r"^(\s*(?:-\s+)?)environment(\s*:)(?:\s+(.*?))?\s*$")
ENV_LIST_ITEM_RE = re.compile(r"^(\s*-\s*[\"']?)([^=\s\"']+)=(.*?)([\"']?)\s*$")
ENV_MAP_ITEM_RE = re.compile(r"^(\s*[\"']?[^:\s\"'#]+[\"']?\s*:)(?:\s+(.*?))?\s*$")
YAML_KEY_RE = re.compile(r"^(?P<lead>[ \t]*(?:-[ \t]+)*)(?P<key>\"[^\"\n]*\"|'[^'\n]*'|[^\s\"'#\[\]{},:-][^\n:#]*?)"
                         r"[ \t]*(?P<colon>:)(?:[ \t]+(?P<value>.*?))?[ \t]*$")
YAML_ITEM_RE = re.compile(r"^(?P<lead>[ \t]*-)(?:[ \t]+(?P<value>.*?))?[ \t]*$")
YAML_NO_VALUE_RE = re.compile(r"^(?:[&!]\S*[ \t]*)*(?:#.*)?$")   # nothing, an anchor or tag, or a comment
YAML_SCALAR_RE = re.compile(r"^(?:[&!]\S*[ \t]+)*[|>][-+0-9]*[ \t]*(?:#.*)?$")
SECRET_WORD_END_RE = re.compile(r"(?:key|token|secret|password|passwd|passphrase|auth|credential|psk)s?$|^pass$",
                                re.IGNORECASE)
WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")
MIN_KNOWN_CHARS = 8

CATEGORIES = ("container_env", "secret_key", "secret_assignment", "secret_argument", "compose_environment",
              "private_key", "jwt", "credential_url", "auth_header", "known_secret")


class Redactor:
    """Removes secrets from probe output and counts what it removed (counts: category -> number)."""

    def __init__(self, known: Iterable[str] = ()) -> None:
        self.counts = {category: 0 for category in CATEGORIES}
        self.known = sorted({value for value in known if isinstance(value, str) and len(value) >= MIN_KNOWN_CHARS},
                            key=len, reverse=True)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    # -- text --------------------------------------------------------------------------

    def text(self, value: str) -> str:
        """value with every text rule applied (see the module docstring)."""
        if not value:
            return value
        value = self._sub(PRIVATE_KEY_RE, lambda m: f"-----BEGIN {m.group(1)}PRIVATE KEY----- {MARK}", value,
                          "private_key")
        value = self._sub(JWT_RE, lambda m: MARK, value, "jwt")
        value = self._sub(URL_USERINFO_RE, lambda m: f"{m.group(1)}{MARK}@", value, "credential_url")
        value = self._sub(AUTH_HEADER_RE, lambda m: f"{m.group(1)} {MARK}", value, "auth_header")
        value = self._sub(QUOTED_COLON_RE, lambda m: m.group(1) + _quoted(m.group(2)), value, "secret_assignment")
        value = self._sub(COLON_PAIR_RE, lambda m: m.group(1) + m.group(2) + m.group(3) + _quoted(m.group(4)), value,
                          "secret_assignment")
        value = self._sub(EQUALS_PAIR_RE, lambda m: m.group(1) + m.group(2) + m.group(3) + _quoted(m.group(4)), value,
                          "secret_assignment")
        value = self._sub(FLAG_ARGUMENT_RE, lambda m: f"{m.group(1)}{m.group(2)}{MARK}", value, "secret_argument")
        value = self._sub(FLAG_LIST_RE, lambda m: m.group(1) + m.group(2) + m.group(1) + m.group(3)
                          + _quoted(m.group(4)), value, "secret_argument")
        for secret in self.known:
            if secret in value:
                self.counts["known_secret"] += value.count(secret)
                value = value.replace(secret, MARK)
        return value

    def compose(self, value: str) -> str:
        """A compose file: the YAML rules (yaml()), and every value of an `environment:` block is removed
        (inline, list or map form; a line in the block that is neither, such as a multi-line value,
        goes whole)."""
        return self._yaml(value, environment=True)

    def yaml(self, value: str) -> str:
        """YAML text (netplan get): every value under a key with a secret word, every line of such a
        key's block scalar and the list item after a secret-looking `- --flag` are removed, then the
        text rules run on every line (see the module docstring)."""
        return self._yaml(value, environment=False)

    def _yaml(self, value: str, environment: bool) -> str:
        out: list[str] = []
        block: tuple[int, str] | None = None    # (column of the key, category): every value under it goes
        scalar: tuple[int, str | None] | None = None   # (column, category or None): a block scalar's lines
        flag: int | None = None                 # column of a `- --secret-flag` item: the next item there goes
        for line in value.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            ending = line[len(body):]
            stripped = body.lstrip(" ")
            indent = len(body) - len(stripped)
            if scalar is not None:
                if not stripped or indent > scalar[0]:
                    if scalar[1] and stripped:
                        self.counts[scalar[1]] += 1
                        out.append(" " * indent + MARK + ending)
                    else:
                        out.append(self.text(body) + ending)
                    continue
                scalar = None
            if not stripped or stripped.startswith("#"):
                out.append(self.text(body) + ending)
                continue
            if block is not None and not (indent > block[0] or (indent == block[0] and stripped.startswith("-"))):
                block = None
            key = YAML_KEY_RE.match(body)
            item = YAML_ITEM_RE.match(body) if key is None else None
            rest = key.group("value") if key else item.group("value") if item else None
            column = len(key.group("lead")) if key else indent   # a block scalar's lines sit right of this
            opens = rest is not None and YAML_SCALAR_RE.match(rest) is not None
            if block is not None:
                if opens:
                    scalar = (column, block[1])
                redacted = self._env_item(body, indent) if block[1] == "compose_environment" else \
                    self._block_item(body, indent, key)
                out.append(self.text(redacted) + ending)
                continue
            argument = (rest or "").strip("\"'")
            if flag is not None and item and indent == flag and argument and not argument.startswith("-"):
                self.counts["secret_argument"] += 1
                out.append(f"{item.group('lead')} {_quoted(rest)}{ending}")
                flag = None
                continue
            flag = indent if item and SECRET_FLAG_RE.match(argument) else None
            match = ENV_BLOCK_RE.match(body) if environment else None
            if match:
                if match.group(3) and not match.group(3).startswith("#"):   # environment: {A: b} or [A=b]
                    self.counts["compose_environment"] += 1
                    out.append(f"{body[:match.end(2)]} {MARK}{ending}")
                    continue
                block = (len(match.group(1)), "compose_environment")
            elif key and secret_name(key.group("key")):
                if rest is None or YAML_NO_VALUE_RE.match(rest):
                    block = (column, "secret_key")
                elif opens:   # the indicator goes too, as inside a block
                    scalar = (column, "secret_key")
                    out.append(f"{body[:key.end('colon')]} {MARK}{ending}")
                    continue
                elif rest.strip("\"'") != MARK:
                    self.counts["secret_assignment"] += 1
                    out.append(f"{body[:key.end('colon')]} {_quoted(rest)}{ending}")
                    continue
            elif opens:
                scalar = (column, None)
            out.append(self.text(body) + ending)
        return "".join(out)

    def _block_item(self, line: str, indent: int, key: re.Match[str] | None) -> str:
        """One line inside a secret key's block: a key keeps its name and loses its value, a list item
        its value; a key or `-` with nothing after it opens a nested block and stays; anything else goes."""
        if key is not None:
            rest = key.group("value")
            if rest is None or YAML_NO_VALUE_RE.match(rest) or rest == MARK:
                return line
            self.counts["secret_key"] += 1
            return f"{line[:key.end('colon')]} {_quoted(rest)}"
        item = YAML_ITEM_RE.match(line)
        if item:
            if not item.group("value") or YAML_NO_VALUE_RE.match(item.group("value")):
                return line
            self.counts["secret_key"] += 1
            return f"{item.group('lead')} {_quoted(item.group('value'))}"
        self.counts["secret_key"] += 1
        return " " * indent + MARK

    def _env_item(self, line: str, indent: int) -> str:
        item = ENV_LIST_ITEM_RE.match(line)
        if item:
            if not item.group(3):
                return line
            self.counts["compose_environment"] += 1
            return f"{item.group(1)}{item.group(2)}={MARK}{item.group(4)}"
        if line.lstrip().startswith("-") and "=" not in line:
            return line   # `- NAME`: passed through from the host, no value here
        pair = ENV_MAP_ITEM_RE.match(line)
        if pair:
            if not pair.group(2) or pair.group(2).startswith("#"):
                return line
            self.counts["compose_environment"] += 1
            return f"{pair.group(1)} {MARK}"
        self.counts["compose_environment"] += 1
        return " " * indent + MARK

    # -- JSON --------------------------------------------------------------------------

    def value(self, obj: Any) -> Any:
        """A parsed JSON value with the structural rules applied (a new object; obj is not changed)."""
        if isinstance(obj, dict):
            return {key: self._entry(key, item) for key, item in obj.items()}
        if isinstance(obj, list):
            return self._list(obj)
        if isinstance(obj, str):
            return self.text(obj)
        return obj

    def _entry(self, key: Any, item: Any) -> Any:
        if key == "Env" and isinstance(item, list):
            return [self._env_value(entry) for entry in item]
        if isinstance(key, str) and NAME_LIKE_RE.match(key) and SECRET_WORD_RE.search(key):
            return self._mask(item)
        return self.value(item)

    def _env_value(self, entry: Any) -> Any:
        if not isinstance(entry, str):
            return self._mask(entry)
        name, sep, rest = entry.partition("=")
        if not sep or not rest:
            return entry
        self.counts["container_env"] += 1
        return f"{name}={MARK}"

    def _list(self, items: list) -> list:
        out: list[Any] = []
        hide_next = False
        for item in items:
            if hide_next and isinstance(item, str) and not item.startswith("-"):
                self.counts["secret_argument"] += 1
                out.append(MARK)
                hide_next = False
                continue
            hide_next = isinstance(item, str) and bool(SECRET_FLAG_RE.match(item))
            out.append(self.value(item))
        return out

    def _mask(self, item: Any) -> Any:
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, dict):
            return {key: self._mask(value) for key, value in item.items()}
        if isinstance(item, list):
            return [self._mask(value) for value in item]
        if item in ("", MARK):
            return item
        self.counts["secret_key"] += 1
        return MARK

    def _sub(self, pattern: re.Pattern[str], replace: Callable[[re.Match[str]], str], value: str,
             category: str) -> str:
        def counted(match: re.Match[str]) -> str:
            self.counts[category] += 1
            return replace(match)
        return pattern.sub(counted, value)


def secret_name(key: str) -> bool:
    """A YAML key whose block is secret: one of its words (split at punctuation and camelCase) ends in
    key, token, secret, password, passwd, passphrase, auth, credential or psk (or plural), or is pass."""
    return any(SECRET_WORD_END_RE.search(word) for word in WORD_SPLIT_RE.split(key.strip("\"'")) if word)


def _quoted(value: str) -> str:
    """MARK, inside the quotes the value had."""
    quote = value[0] if value[:1] in ("'", '"') else ""
    return f"{quote}{MARK}{quote}"
