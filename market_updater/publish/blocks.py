"""Convert the briefing's markdown into Notion blocks.

Supports: ## / ### headings, paragraphs, - bullets, 1. numbered items, > quotes, --- dividers,
pipe tables, and inline **bold**, *italic*, [links](url), [page mentions](notion:<page id>) and
<span color="red">coloured text</span>. Containers are written as:

    ::: toggle Title
    ...markdown...
    :::

    ::: callout 📈 gray
    First line becomes the callout's own text; anything after it goes inside.
    :::

Images: a line of the form ![caption](upload:<file upload id>) or ![caption](https://...).
"""
from __future__ import annotations

import re

INLINE = re.compile(
    r"(<span color=\"(?P<color>[a-z_]+)\">(?P<span>.+?)</span>"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|(?<![\w*])\*(?P<ital>[^*\s][^*]*?)\*(?!\w)"
    r"|\[(?P<ltext>[^\]]+)\]\((?P<url>(?:https?://|notion:)[^)\s]+)\))"
)
IMAGE = re.compile(r"^!\[(?P<cap>[^\]]*)\]\((?P<src>(?:upload:|https?://)[^)\s]+)\)$")
CALLOUT_COLORS = {"gray", "brown", "orange", "yellow", "green", "blue", "purple", "pink", "red", "default"}


def rich_text(s: str, *, bold: bool = False, italic: bool = False, color: str | None = None) -> list[dict]:
    out: list[dict] = []
    pos = 0

    def add(text: str, b=False, i=False, link=None, col=None):
        for k in range(0, len(text), 1900):
            piece = text[k:k + 1900]
            if not piece:
                continue
            obj = {"type": "text", "text": {"content": piece}}
            if link:
                obj["text"]["link"] = {"url": link}
            ann = {}
            if b:
                ann["bold"] = True
            if i:
                ann["italic"] = True
            if col:
                ann["color"] = col
            if ann:
                obj["annotations"] = ann
            out.append(obj)

    for m in INLINE.finditer(s):
        add(s[pos:m.start()], bold, italic, None, color)
        if m.group("span") is not None:
            out.extend(rich_text(m.group("span"), bold=bold, italic=italic, color=m.group("color")))
        elif m.group("bold") is not None:
            out.extend(rich_text(m.group("bold"), bold=True, italic=italic, color=color))
        elif m.group("ital") is not None:
            out.extend(rich_text(m.group("ital"), bold=bold, italic=True, color=color))
        else:
            text, url = m.group("ltext"), m.group("url")
            b = bold
            if text.startswith("**") and text.endswith("**"):
                text, b = text[2:-2], True
            if url.startswith("notion:"):
                obj = {"type": "mention", "mention": {"type": "page", "page": {"id": url[7:]}}}
                out.append(obj)
            else:
                add(text, b, italic, url, color)
        pos = m.end()
    add(s[pos:], bold, italic, None, color)
    return out[:100]


def _block(kind: str, text: str) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": rich_text(text)}}


def _cells(row: str) -> list[str]:
    # Split on pipes that are not inside a <span ...> attribute or a link.
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _table(rows: list[str]) -> dict:
    cells = [_cells(r) for r in rows if not re.fullmatch(r"\|?\s*:?-{2,}.*", r.strip())]
    width = max(len(r) for r in cells)
    children = []
    for r in cells:
        r = r + [""] * (width - len(r))
        children.append({"object": "block", "type": "table_row",
                         "table_row": {"cells": [rich_text(c) for c in r[:width]]}})
    return {"object": "block", "type": "table",
            "table": {"table_width": width, "has_column_header": True, "has_row_header": False,
                      "children": children}}


def _container(lines: list[str], start: int) -> tuple[list[str], int]:
    """Collect the lines inside a ::: block that starts at `start`; return (inner, next index)."""
    depth, j, inner = 1, start + 1, []
    while j < len(lines):
        t = lines[j].strip()
        if t.startswith(":::") and t != ":::":
            depth += 1
        elif t == ":::":
            depth -= 1
            if depth == 0:
                break
        inner.append(lines[j])
        j += 1
    return inner, j + 1


def _image(src: str, caption: str) -> dict:
    if src.startswith("upload:"):
        body = {"type": "file_upload", "file_upload": {"id": src[7:]}}
    else:
        body = {"type": "external", "external": {"url": src}}
    if caption:
        body["caption"] = rich_text(caption)
    return {"object": "block", "type": "image", "image": body}


def to_blocks(md: str) -> list[dict]:
    lines = md.splitlines()
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("::: toggle"):
            title = s[len("::: toggle"):].strip() or "Details"
            inner, i = _container(lines, i)
            blocks.append({"object": "block", "type": "toggle",
                           "toggle": {"rich_text": rich_text(title), "children": to_blocks("\n".join(inner))}})
            continue
        if s.startswith("::: callout"):
            parts = s[len("::: callout"):].split()
            icon = parts[0] if parts else "💡"
            color = parts[1] if len(parts) > 1 and parts[1] in CALLOUT_COLORS else "gray"
            inner, i = _container(lines, i)
            while inner and not inner[0].strip():
                inner.pop(0)
            head = inner.pop(0).strip() if inner else ""
            body = {"rich_text": rich_text(head), "icon": {"type": "emoji", "emoji": icon},
                    "color": "default" if color == "default" else f"{color}_background"}
            kids = to_blocks("\n".join(inner))
            if kids:
                body["children"] = kids
            blocks.append({"object": "block", "type": "callout", "callout": body})
            continue
        if s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            blocks.append(_table(rows))
            continue
        m = IMAGE.match(s)
        if m:
            blocks.append(_image(m.group("src"), m.group("cap")))
        elif s.startswith("### "):
            blocks.append(_block("heading_3", s[4:]))
        elif s.startswith("## "):
            blocks.append(_block("heading_2", s[3:]))
        elif s.startswith("# "):
            blocks.append(_block("heading_1", s[2:]))
        elif re.match(r"^[-*] ", s):
            blocks.append(_block("bulleted_list_item", s[2:]))
        elif re.match(r"^\d+[.)] ", s):
            blocks.append(_block("numbered_list_item", re.sub(r"^\d+[.)] ", "", s)))
        elif s.startswith("> "):
            blocks.append(_block("quote", s[2:]))
        elif s == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        else:
            blocks.append(_block("paragraph", s))
        i += 1
    return blocks


def depth(block: dict) -> int:
    """Levels in a block tree: a plain block is 1, a toggle holding paragraphs is 2."""
    kids = block.get(block["type"], {}).get("children") or []
    return 1 + max((depth(k) for k in kids), default=0)
