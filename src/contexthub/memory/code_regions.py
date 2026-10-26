"""Locate explicit code markup and recognizable bare conditional code lines.

Offsets refer to the unmodified message. This is a syntax filter, not a decision
about whether surrounding prose establishes a user rule.
"""

import ast
import re

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HTML_CODE = re.compile(r"<(pre|code)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
_IF = re.compile(r"\bif\b")
_BRACED_IF = re.compile(r"^\s*(?:}\s*else\s+)?if\b[^\n]*\{")
_BLOCK_HEADER = re.compile(r":\s*(?:#.*)?$")


def _python_line(text):
    text = text.strip()
    if _BLOCK_HEADER.search(text):
        text += "\n    pass"
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def code_regions(source):
    """Return excluded [start, end) spans without removing any source text."""
    regions = []
    lines = source.splitlines(keepends=True)
    offset, opened = 0, None
    for line in lines:
        match = _FENCE.match(line.rstrip("\r\n"))
        if match:
            marker, suffix = match.groups()
            if opened is None:
                # Backticks in the info string invalidate a backtick opener.
                if marker[0] != "`" or "`" not in suffix:
                    opened = (offset, marker)
            elif marker[0] == opened[1][0] and len(marker) >= len(opened[1]) and not suffix.strip():
                regions.append((opened[0], offset + len(line)))
                opened = None
        offset += len(line)
    if opened is not None:
        regions.append((opened[0], len(source)))
    for pattern in (_HTML_CODE, _INLINE_CODE):
        regions.extend(match.span() for match in pattern.finditer(source))

    offset = 0
    for index, line in enumerate(lines):
        if _IF.search(line) and not any(start <= offset < end for start, end in regions):
            tree = _python_line(line)
            # A bare Python block header needs a syntactic, indented body;
            # a prose heading such as "if hungry:" alone is not enough.
            if _BLOCK_HEADER.search(line.rstrip()):
                following = next((s for s in lines[index + 1:] if s.strip()), "")
                indent = len(line.expandtabs()) - len(line.expandtabs().lstrip())
                next_indent = len(following.expandtabs()) - len(following.expandtabs().lstrip())
                if next_indent <= indent or _python_line(following) is None:
                    tree = None
            if _BRACED_IF.match(line) or (tree is not None and any(
                isinstance(node, (ast.If, ast.IfExp))
                or isinstance(node, ast.comprehension) and node.ifs
                for node in ast.walk(tree)
            )):
                regions.append((offset, offset + len(line)))
        offset += len(line)
    return regions
