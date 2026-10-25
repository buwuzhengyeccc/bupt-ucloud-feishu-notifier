import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def safe_url(value):
    parts = urlsplit(value)
    path = re.sub(r'[A-Za-z0-9_-]{48,}', '<redacted>', parts.path)
    query = urlencode([(key, '<redacted>') for key, _ in parse_qsl(parts.query)])
    return urlunsplit((parts.scheme, parts.hostname or '', path, query, ''))


def shape(value, depth=0):
    if depth >= 7:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(k) if len(str(k)) < 65 else '<key>': shape(v, depth + 1)
                for k, v in list(value.items())[:100]}
    if isinstance(value, list):
        return {'type': 'list', 'length': len(value), 'item': shape(value[0], depth + 1) if value else None}
    return type(value).__name__


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
