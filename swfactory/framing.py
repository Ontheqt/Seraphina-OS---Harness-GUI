"""Cleaning and framing of text the harness did not write (Wake & Messages S38), shared by the wake turns
(swfactory/wakes.py) and the browser adapter (swfactory/browser, BROWSER_AND_CODEX.md §2.3).

clean_text removes Unicode format characters, the tag block and every known chat-template control token;
escape_markers escapes the context's own block markers; frame puts outside text inside a <factory-data> block that
names its source and taint; cap cuts a page-derived string to a length. Standard library only and no imports from
the rest of the package: the browser's shim imports it inside its own namespace (§2.9).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

_SPECIAL_TOKENS = ("<|im_start|>", "<|im_end|>", "<|endoftext|>", "<|eot_id|>", "<|start_header_id|>",
                   "<|end_header_id|>", "<|begin_of_text|>", "<|system|>", "<|user|>", "<|assistant|>",
                   "<|channel|>", "<|message|>", "<|start|>", "<|end|>", "<|return|>", "<｜User｜>",
                   "<｜Assistant｜>", "<｜end▁of▁sentence｜>", "[INST]", "[/INST]", "<<SYS>>", "<</SYS>>",
                   "[SYSTEM_PROMPT]", "[/SYSTEM_PROMPT]", "[TOOL_CALLS]", "[AVAILABLE_TOOLS]", "[/AVAILABLE_TOOLS]",
                   "[TOOL_RESULTS]", "[/TOOL_RESULTS]", "<start_of_turn>", "<end_of_turn>", "<tool_call>",
                   "</tool_call>", "<tool_response>", "</tool_response>", "<think>", "</think>",
                   # SentencePiece and Llama 2 / Mistral / Gemma control tokens that have no bars
                   "<s>", "</s>", "<bos>", "<eos>", "<pad>", "<unk>", "<mask>", "<EOT>", "<start_of_image>",
                   "<end_of_image>", "<image_soft_token>", "<start_of_audio>", "<end_of_audio>",
                   "<audio_soft_token>")


def _variants(token: str) -> set[str]:
    out = {token}
    for old, new in (("|", "｜"), ("｜", "|"), ("_", "▁"), ("▁", "_")):
        out |= {variant.replace(old, new) for variant in list(out)}
    return out


_TOKEN_RE = re.compile("|".join(sorted((re.escape(v) for t in _SPECIAL_TOKENS for v in _variants(t)), key=len,
                                       reverse=True)), re.IGNORECASE)
_ANY_BAR_TOKEN_RE = re.compile(r"<[|｜][^<>\n]{0,32}?[|｜]>")


def clean_text(text: str) -> str:
    """Text the code did not write, made safe for a self-hosted model (S38): Unicode format characters (Cf:
    zero-width and direction marks) and the tag block U+E0000-U+E007F removed, then every known chat-template
    control token (ChatML, Llama 3, DeepSeek's fullwidth forms, Mistral, Gemma, Qwen tool tags, think tags; with
    "|" or "｜" and "_" or "▁") and any <|...|> or <｜...｜> token of up to 32 characters, until none is left."""
    cleaned = "".join(ch for ch in str(text or "") if unicodedata.category(ch) != "Cf"
                      and not 0xE0000 <= ord(ch) <= 0xE007F)
    while True:
        stripped = _ANY_BAR_TOKEN_RE.sub("", _TOKEN_RE.sub("", cleaned))
        if stripped == cleaned:
            return cleaned
        cleaned = stripped


_FRAME_OPEN_RE = re.compile(r"<(/?)(factory-data|wake|consolidation)(?![\w-])", re.IGNORECASE)
LOOKALIKES = str.maketrans({"<": "‹", ">": "›", "[": "⁅", "]": "⁆", "|": "¦"})


def escape_markers(text: str) -> str:
    """The context's own markers in text it did not write (<factory-data, <wake, <consolidation and their closing
    forms) escaped to <...-quoted, so no text can end or open a block."""
    return _FRAME_OPEN_RE.sub(lambda m: f"<{m.group(1)}{m.group(2)}-quoted", str(text))


def inline(text: str) -> str:
    """Text shown outside a block (the owner's own summary, a clean note, a clean reply): cleaned and escaped."""
    return escape_markers(clean_text(text))


def frame(source: str, text: str, cap: int, taint: Iterable[str] = ("untrusted",), *, lookalike: bool = False) -> str:
    """Outside text as data (S38): cleaned, cut to cap, its own <factory-data, <wake and <consolidation markers
    (and their closing forms) escaped to <...-quoted, inside a block that names its source and taint. lookalike:
    the re-render used when the tokenizer still finds a control token (<, >, [, ] and | replaced by look-alike
    characters)."""
    body = clean_text(text)
    if len(body) > cap:
        body = body[:max(0, cap - 1)] + "…"
    body = escape_markers(body)
    if lookalike:
        body = body.translate(LOOKALIKES)
    labels = " ".join(sorted(set(taint))) or "clean"
    safe_source = re.sub(r"[^a-z0-9._-]", "-", str(source).lower())[:40]
    return f'<factory-data source="{safe_source}" taint="{labels}">\n{body}\n</factory-data>'


def cap(text: object, limit: int) -> str:
    """A page-derived string cleaned (clean_text), its line breaks and tabs made spaces and cut to limit characters
    (an ellipsis marks the cut): a URL, a title, a dialog's message or a file name (BROWSER_AND_CODEX.md §2.3)."""
    one = " ".join(clean_text(str(text if text is not None else "")).split())
    return one if len(one) <= limit else one[:max(0, limit - 1)] + "…"
