"""One local, short-lived render process. Never executes remote commands or SVG scripts."""
from __future__ import annotations

import json
import math
import os
from contextlib import closing
from pathlib import Path
import re
import sys
import warnings
import xml.etree.ElementTree as ET


def svg_source(source: Path) -> tuple[str, int, int]:
    text = source.read_text(encoding='utf-8-sig')
    if '<!ENTITY' in text.upper():
        raise ValueError('SVG entity declarations are not supported in previews')
    root = ET.fromstring(text)
    if root.tag.rsplit('}', 1)[-1] != 'svg':
        raise ValueError('Not an SVG document')
    # Self-contained plots only: do not load any local/remote resources while rendering.
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1].lower() in {'script', 'foreignobject', 'iframe', 'object', 'embed'}:
            raise ValueError('Active SVG content is not supported in previews')
        for key, value in node.attrib.items():
            if key.rsplit('}', 1)[-1].lower() == 'href' and not (
                value.startswith('#') or re.match(r'^data:image/(?:png|jpeg|gif|webp);base64,', value, re.I)
            ):
                raise ValueError('External SVG resources are not supported in previews')
        for value in [*node.attrib.values(), node.text or '']:
            # Decode CSS escapes before checking resource references.
            value = re.sub(r'\\([0-9a-fA-F]{1,6})\s?|\\([^\r\n])',
                           lambda match: chr(int(match[1], 16)) if match[1] else match[2], value)
            if '@import' in value.lower():
                raise ValueError('External SVG styles are not supported in previews')
            for match in re.finditer(r'url\((.*?)\)', value, re.I | re.S):
                if not match[1].strip().strip('\'"').startswith('#'):
                    raise ValueError('External SVG resources are not supported in previews')
    box = root.attrib.get('viewBox', '').replace(',', ' ').split()
    if len(box) == 4:
        width, height = float(box[2]), float(box[3])
    else:
        def dimension(name, default):
            match = re.match(r'^\s*([0-9.]+)', root.attrib.get(name, ''))
            return float(match[1]) if match else default
        width, height = dimension('width', 300), dimension('height', 150)
    if not all(math.isfinite(x) and x > 0 for x in (width, height)):
        raise ValueError('Invalid SVG dimensions')
    scale = min(2, 2048 / max(width, height))
    return ET.tostring(root, encoding='unicode'), max(1, round(width * scale)), max(1, round(height * scale))


def render(source: Path, kind: str, page_number: int) -> dict:
    target = source.with_name('preview.png')
    if kind == 'svg':
        import resvg_py
        svg, width, height = svg_source(source)
        data = resvg_py.svg_to_bytes(svg_string=svg, width=width, height=height)
        with open(target, 'xb', opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
            output.write(data)
        return {'preview_name': target.name, 'width': width, 'height': height}
    from PIL import Image
    if kind == 'pdf':
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(source) as document:
            pages = len(document)
            if not 1 <= page_number <= pages:
                raise ValueError(f'PDF page must be between 1 and {pages}')
            with closing(document[page_number - 1]) as page:
                width, height = page.get_size()
                if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                    raise ValueError('Invalid PDF page dimensions')
                scale = min(2, 2048 / max(width, height))
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil()
                    with open(target, 'xb', opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
                        image.save(output, format='PNG')
                    width, height = image.size
                finally:
                    bitmap.close()
        return {'preview_name': target.name, 'page': page_number, 'pages': pages,
                'width': width, 'height': height}
    Image.MAX_IMAGE_PIXELS = 40_000_000
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(source) as image:
            width, height = image.size
            image.verify()
    return {'preview_name': source.name, 'width': width, 'height': height}


def main():
    try:
        value = render(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]))
    except ImportError:
        value = {'preview_error': 'Install Remote Workbench with the [images] extra for local previews.'}
    except Exception as exc:
        value = {'preview_error': str(exc)[:240] or 'Local image preview failed.'}
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    main()
