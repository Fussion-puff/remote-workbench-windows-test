"""Fetch one remote image; keep all binary data outside MCP responses."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from remote_workbench import audit
from remote_workbench.remote_client import edit


def image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', 'jpg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif', 'gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp', 'webp'
    if data.lstrip().startswith(b'%PDF-'):
        return 'application/pdf', 'pdf'
    if re.search(rb'<(?:[A-Za-z_][\w.-]*:)?svg(?:\s|>)', data[:16384]):
        return 'image/svg+xml', 'svg'
    raise ValueError('Supported image contents: PNG, JPG, PDF, SVG, GIF, WebP')


def render_preview(source: Path, kind: str, page: int) -> dict:
    env = dict(os.environ, PYTHONNOUSERSITE='1')
    for name in ('PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'DYLD_INSERT_LIBRARIES'):
        env.pop(name, None)
    try:
        result = subprocess.run(
            [sys.executable, '-I', '-m', 'remote_workbench.image_render', str(source), kind, str(page)],
            cwd=source.parent, env=env, capture_output=True, text=True,
            timeout=20, start_new_session=True,
        )
        if result.returncode:
            return {'preview_error': 'Local preview failed; the original file is available.'}
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError('Invalid preview metadata')
        return value
    except subprocess.TimeoutExpired:
        return {'preview_error': 'Local preview timed out; the original file is available.'}
    except (OSError, ValueError):
        return {'preview_error': 'Local preview is unavailable; the original file is available.'}


async def fetch_image(executor, path: str, page: int) -> dict:
    value = await edit(executor, {'op': 'read_binary', 'path': path}, max_bytes=256)
    output_id = value.get('output_id')
    if value.get('error'):
        return {'error': value['error'], 'message': str(value.get('message', 'Image fetch failed'))[:300],
                'output_id': output_id}
    try:
        data = base64.b64decode(value.pop('data_base64'), validate=True)
        if len(data) != value['size'] or hashlib.sha256(data).hexdigest() != value['sha256']:
            raise ValueError('Image transfer integrity check failed')
        mime, extension = image_type(data)
    except (ValueError, KeyError, TypeError) as exc:
        return {'error': 'invalid_image', 'message': str(exc)[:300], 'output_id': output_id}
    if extension != 'pdf' and page != 1:
        return {'error': 'invalid_page', 'message': 'page is only selectable for PDF files', 'output_id': output_id}
    record_id = audit.operation_id.get() or output_id
    directory = (audit.directory(record_id) / 'image').absolute()
    directory.mkdir(mode=0o700)
    source = directory / ('source.' + extension)
    with open(source, 'xb', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        stream.write(data)
    preview = await asyncio.to_thread(render_preview, source, extension, page)
    if (extension in {'png', 'jpg', 'gif', 'webp'}
            and preview.get('preview_error', '').startswith('Install Remote Workbench')):
        # Raster files can be displayed natively without an optional renderer.
        preview = {'preview_name': source.name}
    # A renderer can only select these two fixed files within the new cache.
    preview_name = preview.pop('preview_name', None)
    preview_path = directory / preview_name if preview_name in {source.name, 'preview.png'} else None
    if preview_path is not None and (not preview_path.is_file() or preview_path.is_symlink()):
        preview_path = None
        preview = {'preview_error': 'Local preview did not produce an image.'}
    result = {'success': True, 'local_path': str(source), 'mime_type': mime,
              'size': len(data), 'output_id': output_id, **preview}
    if preview_path is not None:
        result['preview_path'] = str(preview_path)
        result['image_markdown'] = f'![远端图片](<{preview_path}>)'
    audit.update(record_id, image={key: result[key] for key in result if key != 'image_markdown'},
                 remote_image_path=path, image_sha256=value['sha256'])
    return result
