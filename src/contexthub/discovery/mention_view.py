"""Candidate-only dialogue views; offsets always address unchanged raw sources."""
import json
import re
from html.parser import HTMLParser

from contexthub.models.knowledge import ContractError


class _TextNodes(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.lines = [0] + [m.end() for m in re.finditer('\n', text)]
        self.ranges = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data:
            line, column = self.getpos()
            start = self.lines[line - 1] + column
            self.ranges.append((start, start + len(data)))


def _decoded_string(raw, start, end):
    """Decode JSON escapes with one raw interval per decoded Unicode character."""
    chars, spans = [], []
    pos = start + 1
    while pos < end - 1:
        stop = pos + 1
        if raw[pos] == '\\':
            stop = pos + (6 if raw[pos + 1] == 'u' else 2)
            if raw[pos + 1] == 'u' and 0xD800 <= int(raw[pos + 2:stop], 16) <= 0xDBFF:
                if raw[stop:stop + 2] == '\\u' and 0xDC00 <= int(raw[stop + 2:stop + 6], 16) <= 0xDFFF:
                    stop += 6
        value = json.loads('"' + raw[pos:stop] + '"')
        chars.append(value)
        spans.extend([(pos, stop)] * len(value))
        pos = stop
    return ''.join(chars), spans


def dialogue_views(raw):
    """Require complete serialized dialogue; never guess a truncated fragment."""
    decoder = json.JSONDecoder()
    try:
        data = json.loads(raw)
        if not isinstance(data, list) or not data or any(
            not isinstance(item, dict) or not isinstance(item.get('role'), str)
            or not isinstance(item.get('content'), str) for item in data
        ):
            raise ValueError()
        # Walk the validated top-level array and each object's key/value pairs.
        pos = raw.index('[') + 1
        views = []
        def skip(p):
            while p < len(raw) and raw[p].isspace():
                p += 1
            return p
        for item in data:
            pos = skip(pos)
            if raw[pos] == ',':
                pos = skip(pos + 1)
            if raw[pos] != '{':
                raise ValueError()
            pos += 1
            keys = set()
            while True:
                pos = skip(pos)
                if raw[pos] == '}':
                    pos += 1
                    break
                key, pos = decoder.raw_decode(raw, pos)
                if key in keys:
                    raise ValueError()  # duplicate keys are ambiguous
                keys.add(key)
                pos = skip(pos)
                if raw[pos] != ':':
                    raise ValueError()
                start = skip(pos + 1)
                _, pos = decoder.raw_decode(raw, start)
                if key == 'content':
                    text, spans = _decoded_string(raw, start, pos)
                    parser = _TextNodes(text)
                    parser.feed(text)
                    parser.close()
                    views.extend((text[a:b], spans[a:b]) for a, b in parser.ranges)
                pos = skip(pos)
                if raw[pos] == ',':
                    pos += 1
        return views
    except (ValueError, IndexError, TypeError) as exc:
        raise ContractError('invalid_complete_dialogue_for_mentions') from exc
