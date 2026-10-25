"""Images travel over simulated SSH once; MCP only carries local paths/metadata."""
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from remote_workbench import audit, image_cache
from remote_workbench.image_render import svg_source
from remote_workbench.tools import files
from remote_workbench.workspace import Workspace, create_server


def plot(root, kind):
    path = root / ("空 格 ' $(touch unintended)." + kind)
    if kind == 'svg':
        pytest.importorskip('resvg_py')
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 100"><rect width="300" height="100" fill="white"/><path d="M10 90 L150 10 L290 90" fill="none" stroke="blue" stroke-width="4"/></svg>')
    else:
        Image = pytest.importorskip('PIL.Image')
        image = Image.new('RGB', (300, 100), '#3165d7')
        if kind == 'pdf':
            pytest.importorskip('pypdfium2')
            image.save(path, save_all=True, append_images=[Image.new('RGB', (200, 100), '#c54040')])
        else:
            image.save(path)
    return path


@pytest.mark.parametrize('kind', ['png', 'jpg', 'pdf', 'svg'])
async def test_fetch_image_preserves_original_and_returns_chat_preview(local, kind):
    _, root = local
    path = plot(root, kind)
    value = await files.ssh_fetch_image(path.name, page=2 if kind == 'pdf' else 1)
    assert value.get('success'), value
    assert Path(value['local_path']).read_bytes() == path.read_bytes()
    preview = Path(value['preview_path'])
    assert preview.is_file() and preview.suffix == ('.jpg' if kind == 'jpg' else '.png')
    assert value['image_markdown'] == f'![远端图片](<{preview}>)'
    assert root / 'audit' in preview.parents
    assert os.stat(value['local_path']).st_mode & 0o777 == 0o600
    assert os.stat(preview).st_mode & 0o777 == 0o600
    assert value['width'] > 0 and value['height'] > 0
    assert not (root / 'unintended').exists()
    assert 'data_base64' not in json.dumps(value)
    assert len(json.dumps(value).encode()) < 4096
    if kind == 'pdf':
        assert value['page'] == value['pages'] == 2
    record = json.loads((audit.directory(value['operation_id']) / 'record.json').read_text())
    assert record['image']['preview_path'] == value['preview_path']
    assert record['remote_image_path'] == path.name


async def test_workspace_mcp_fetch_image_is_one_compact_text_result(local):
    executor, root = local
    path = plot(root, 'png')
    server = create_server(Workspace('images', 'remote', str(root), executor), viewer_url='http://127.0.0.1:12345/v/example/')
    blocks = await server.call_tool('fetch_image', {'path': path.name})
    assert len(blocks) == 1 and blocks[0].type == 'text'
    value = json.loads(blocks[0].text)
    assert value['image_markdown'].startswith('![远端图片](</')
    assert 'Remote Workbench 操作记录' in value['operation_log']
    assert 'data_base64' not in blocks[0].text
    assert 'preview_url' not in value


async def test_chats_keep_independent_immutable_image_copies(local):
    _, root = local
    path = plot(root, 'png')
    with audit.scoped_root(root / 'chat-a'):
        first = await files.ssh_fetch_image(path.name)
    original = Path(first['local_path']).read_bytes()
    Image = pytest.importorskip('PIL.Image')
    Image.new('RGB', (100, 100), '#ee5500').save(path)
    with audit.scoped_root(root / 'chat-b'):
        second = await files.ssh_fetch_image(path.name)
    assert first['local_path'] != second['local_path']
    assert Path(first['local_path']).read_bytes() == original
    assert Path(second['local_path']).read_bytes() != original


@pytest.mark.parametrize('page', [0, -1, True, 1.5])
async def test_invalid_page_never_reaches_ssh(local, page):
    executor, _ = local
    with patch.object(executor, 'run') as run:
        with pytest.raises(ValueError, match='positive integer'):
            await files.ssh_fetch_image('plot.pdf', page=page)
        run.assert_not_called()


@pytest.mark.parametrize('kind', ['missing', 'text', 'large', 'fifo'])
async def test_fetch_rejects_missing_non_image_oversized_and_nonregular_sources(local, kind):
    executor, root = local
    path = root / 'source.png'
    if kind == 'text':
        path.write_text('This is text, not an image.')
    elif kind == 'large':
        executor.config = replace(executor.config, max_file_size=32)
        path.write_bytes(b'\x89PNG\r\n\x1a\n' + b'x' * 50)
    elif kind == 'fifo':
        os.mkfifo(path)
    value = await files.ssh_fetch_image(path.name)
    assert value.get('error'), value
    assert 'image_markdown' not in value and 'preview_path' not in value
    assert 'data_base64' not in json.dumps(value)


async def test_pdf_invalid_page_preserves_original_without_broken_chat_image(local):
    _, root = local
    path = plot(root, 'pdf')
    value = await files.ssh_fetch_image(path.name, page=3)
    assert Path(value['local_path']).read_bytes() == path.read_bytes()
    assert 'between 1 and 2' in value['preview_error']
    assert 'image_markdown' not in value


async def test_render_failure_still_returns_original_file(local, monkeypatch):
    _, root = local
    path = plot(root, 'png')
    monkeypatch.setattr(image_cache, 'render_preview', lambda *args: {'preview_error': 'Local preview timed out'})
    value = await files.ssh_fetch_image(path.name)
    assert value['success'] and Path(value['local_path']).read_bytes() == path.read_bytes()
    assert 'image_markdown' not in value
    assert value['preview_error'] == 'Local preview timed out'


async def test_png_can_display_without_optional_rendering_packages(local, monkeypatch):
    _, root = local
    path = plot(root, 'png')
    monkeypatch.setattr(image_cache, 'render_preview', lambda *args: {
        'preview_error': 'Install Remote Workbench with the [images] extra for local previews.'})
    value = await files.ssh_fetch_image(path.name)
    assert value['preview_path'] == value['local_path']
    assert value['image_markdown'] and 'preview_error' not in value


@pytest.mark.parametrize('content', [
    '<image href="file:///private/secret.png"/>',
    '<image href="https://example.com/plot.png"/>',
    '<style>@import "https://example.com/font.css";</style>',
    '<rect fill="url(file:///private/secret.svg)"/>',
    '<rect style="fill:u\\72l(file:///private/secret.svg)"/>',
    '<script>alert(1)</script>',
    '<foreignObject><div>active</div></foreignObject>',
])
def test_svg_preview_does_not_load_external_or_active_content(tmp_path, content):
    source = tmp_path / 'source.svg'
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg">' + content + '</svg>')
    with pytest.raises(ValueError):
        svg_source(source)


def test_svg_preview_accepts_self_contained_plot_and_bounds_dimensions(tmp_path):
    source = tmp_path / 'source.svg'
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 30000 10000"><defs><clipPath id="clip"><rect width="2" height="2"/></clipPath></defs><path clip-path="url(#clip)" d="M0 0 L1 1"/></svg>')
    svg, width, height = svg_source(source)
    assert 'clip' in svg and width == 2048 and 0 < height <= 2048
