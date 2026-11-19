"""Convert the briefing's markdown into Notion blocks.

Supports: ## / ### headings, paragraphs, - bullets, 1. numbered items, **bold**, *italic*,
[links](url), pipe tables, and toggles written as:
    ::: toggle Title
    ...markdown...
    :::
"""
from __future__ import annotations

import re

INLINE = re.compile(r"(\*\*(.+?)\*\*|\*(.+?)\*|\[([^\]]+)\]\((https?://[^)\s]+)\))")


def rich_text(s: str) -> list[dict]:
    out: list[dict] = []
    pos = 0

    def add(text: str, bold=False, italic=False, link=None):
        for i in range(0, len(text), 1900):
            piece = text[i:i + 1900]
            if not piece:
                continue
            obj = {"type": "text", "text": {"content": piece}}
            if link:
                obj["text"]["link"] = {"url": link}
            if bold or italic:
                obj["annotations"] = {"bold": bold, "italic": italic}
            out.append(obj)

    for m in INLINE.finditer(s):
        add(s[pos:m.start()])
        if m.group(2) is not None:
            add(m.group(2), bold=True)
        elif m.group(3) is not None:
            add(m.group(3), italic=True)
        else:
            add(m.group(4), link=m.group(5))
        pos = m.end()
    add(s[pos:])
    return out[:100]


def _block(kind: str, text: str) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": rich_text(text)}}


def _table(rows: list[str]) -> dict:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows
             if not re.fullmatch(r"\|?\s*:?-{2,}.*", r.strip())]
    width = max(len(r) for r in cells)
    children = []
    for r in cells:
        r = r + [""] * (width - len(r))
        children.append({"object": "block", "type": "table_row",
                         "table_row": {"cells": [rich_text(c) for c in r[:width]]}})
    return {"object": "block", "type": "table",
            "table": {"table_width": width, "has_column_header": True, "has_row_header": False,
                      "children": children}}


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
            depth, j, inner = 1, i + 1, []
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
            children = to_blocks("\n".join(inner))
            blocks.append({"object": "block", "type": "toggle",
                           "toggle": {"rich_text": rich_text(title), "children": children[:100]}})
            i = j + 1
            continue
        if s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            blocks.append(_table(rows))
            continue
        if s.startswith("### "):
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
