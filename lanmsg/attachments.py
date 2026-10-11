"""Local attachment storage uses opaque IDs, never user-provided filesystem paths."""

import re
import unicodedata
from pathlib import Path

ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def attachment_directory(database):
    database = Path(database)
    return database.with_name(database.name + ".attachments")


def clean_filename(value):
    value = unicodedata.normalize('NFC', value).strip()
    if not value or value in ('.', '..') or len(value) > 180 or any(char in '/\\:' or unicodedata.category(char).startswith('C') for char in value):
        raise ValueError('Use a filename without paths or control characters (180 characters maximum)')
    return value


def preview_type(header, filename):
    suffix = Path(filename).suffix.lower()
    if suffix == '.png' and header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if suffix in ('.jpg', '.jpeg') and header.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if suffix == '.gif' and header.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if suffix == '.webp' and header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return 'image/webp'
    if suffix == '.mp4' and header[4:8] == b'ftyp':
        return 'video/mp4'
    if suffix == '.webm' and header.startswith(b'\x1a\x45\xdf\xa3'):
        return 'video/webm'
    return 'application/octet-stream'
