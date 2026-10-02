"""Deterministic, lossless source partitions for model-selected evidence IDs.

No decoding, normalization, semantic filtering, or labels enter this view.
Concatenating units reconstructs every original source character exactly.
"""
import re

MAX_UNIT_CHARACTERS = 512
UNIT_VERSION = 'raw-partition-512-v1'


def partition_source(text):
    start = 0
    while start < len(text):
        limit = min(start + MAX_UNIT_CHARACTERS, len(text))
        boundary = re.search(r'\\n|\n', text[start:limit])
        if boundary is not None:
            end = start + boundary.end()
        elif limit < len(text):
            space = text.rfind(' ', start + MAX_UNIT_CHARACTERS // 2, limit)
            end = space + 1 if space >= 0 else limit
        else:
            end = limit
        yield start, end, text[start:end]
        start = end


def evidence_unit_view(sources):
    """Return model view and trusted registry in supplied source order."""
    view, registry = {}, {}
    for source_id, text in sources.items():
        units = []
        for start, end, excerpt in partition_source(text):
            ident = f'E{len(registry) + 1:06d}'
            registry[ident] = dict(source_id=source_id, start=start, end=end,
                                   excerpt=excerpt,
                                   provenance=(UNIT_VERSION, ident))
            units.append(dict(id=ident, text=excerpt))
        view[source_id] = units
    return view, registry
