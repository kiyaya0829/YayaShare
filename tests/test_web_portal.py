"""Real HTTP requests, isolated storage, and interruption/security regressions."""
import http.client
import json
from pathlib import Path
import socket
import time
from urllib.parse import quote

import pytest

from yayashare.storage import State
from yayashare import web_portal as web


@pytest.fixture
def portal(tmp_path):
    instance = web.WebPortal(State(tmp_path / 'state'), ['127.0.0.1'], host='127.0.0.1', port=0)
    try:
        yield instance
    finally:
        instance.stop()


def request(portal, path, body=None, *, cookie=None, headers=None, method=None):
    method = method or ('POST' if body is not None else 'GET')
    base = {'Origin': f'http://127.0.0.1:{portal.port}', 'X-YayaShare': '1'}
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        base['Content-Type'] = 'application/json'
    if cookie:
        base['Cookie'] = cookie
    base.update(headers or {})
    conn = http.client.HTTPConnection('127.0.0.1', portal.port, timeout=3)
    try:
        conn.request(method, path, body, base)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def login(portal):
    status, headers, _ = request(portal, '/api/connect', {'token': portal.invite})
    assert status == 200
    assert 'HttpOnly' in headers['Set-Cookie'] and 'SameSite=Strict' in headers['Set-Cookie']
    return headers['Set-Cookie'].split(';')[0]


def eventually(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    assert predicate()


def test_assets_auth_single_use_and_private_history(portal):
    portal.state.record('text', 'private desktop', 'private history secret')
    for path in ('/', '/app.js', '/style.css', '/icon.png'):
        status, headers, body = request(portal, path)
        assert status == 200 and body
        assert headers['Cache-Control'] == 'no-store'
        assert headers['X-Content-Type-Options'] == 'nosniff'
        assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
        assert portal.invite.encode() not in body
    assert request(portal, '/api/items')[0] == 401
    invite = portal.invite
    cookie = login(portal)
    assert request(portal, '/api/connect', {'token': invite})[0] == 403
    assert request(portal, '/api/items', cookie='yaya_web=wrong')[0] == 401
    status, _, body = request(portal, '/api/items', cookie=cookie)
    assert status == 200 and json.loads(body)['items'] == []
    assert b'private history secret' not in body
    for path in ('/state.json', '/../state.json', '/%2e%2e/state.json', '/download/../../state.json'):
        assert request(portal, path, cookie=cookie)[0] == 404


@pytest.mark.parametrize('token', ['', None, 42, '喵', 'x' * 1000])
def test_invalid_invites(portal, token):
    assert request(portal, '/api/connect', {'token': token})[0] == 403
    assert portal.invite


@pytest.mark.parametrize('cookie', ['yaya_web="é"', 'bad cookie;', 'yaya_web="unterminated', '=bad'])
def test_malformed_cookie(portal, cookie):
    login(portal)
    assert request(portal, '/api/items', cookie=cookie)[0] == 401


@pytest.mark.parametrize('headers', [
    {'Host': 'evil.example'}, {'Origin': 'https://evil.example'},
    {'Origin': 'null'}, {'Origin': ''}, {'X-YayaShare': ''}, {'Sec-Fetch-Site': 'cross-site'},
])
def test_cross_site_and_dns_rebinding_rejected(portal, headers):
    cookie = login(portal)
    assert request(portal, '/api/text', {'text': 'do not save'}, cookie=cookie, headers=headers)[0] == 403
    assert not portal.state.history()


def test_text_both_directions_and_size_limit(portal):
    cookie = login(portal)
    content = '你好 <script>alert(1)</script> 🐾\nclipboard'
    assert request(portal, '/api/text', {'text': content}, cookie=cookie)[0] == 200
    assert portal.state.history()[0]['content'] == content
    assert portal.state.history()[0]['sender'] == '手机网页'
    portal.share_text(content)
    listing = json.loads(request(portal, '/api/items', cookie=cookie)[2])['items']
    assert listing[0]['text'] == content
    assert request(portal, '/api/text', {'text': '喵' * (web.MAX_TEXT // 3 + 1)}, cookie=cookie)[0] == 413
    assert len(portal.state.history()) == 1


@pytest.mark.parametrize('filename', ['../../secret.txt', 'C:\\Users\\x\\CON.txt', '你好 🐾.bin', '.partial'])
@pytest.mark.parametrize('data', [b'', b'abc\x00' * 200000], ids=['empty', 'binary-800KB'])
def test_upload_sanitizes_names_and_never_overwrites(portal, filename, data):
    cookie = login(portal)
    for _ in range(2):
        assert request(portal, '/api/file', data, cookie=cookie, headers={'X-Filename': quote(filename)})[0] == 200
    paths = [Path(item['content']) for item in portal.state.history()]
    assert len(set(paths)) == 2
    assert all(path.is_relative_to(portal.state.inbox) and path.read_bytes() == data for path in paths)
    assert all(len(path.relative_to(portal.state.inbox).parts) == 2 for path in paths)


def test_export_is_snapshot_and_download_attachment(portal, tmp_path):
    cookie = login(portal)
    source = tmp_path / '你好.html'
    source.write_bytes(b'<script>original</script>')
    item_id = portal.share_file(source)
    source.write_bytes(b'changed')
    status, headers, body = request(portal, '/download/' + item_id, cookie=cookie)
    assert status == 200 and body == b'<script>original</script>'
    assert headers['Content-Type'] == 'application/octet-stream'
    assert headers['Content-Disposition'].startswith('attachment;')
    assert quote(source.name) in headers['Content-Disposition']
    assert 'path' not in portal.listing()[0]
    assert request(portal, '/download/' + item_id)[0] == 401
    spool = portal.spool
    portal.stop()
    assert not spool.exists() and source.read_bytes() == b'changed'
    assert not portal.authenticated(cookie)


def test_expiration_revokes_existing_cookie(portal):
    cookie = login(portal)
    portal.share_text('expires')
    portal.expires = time.monotonic() - 1
    assert request(portal, '/api/items', cookie=cookie)[0] == 403
    assert request(portal, '/api/text', {'text': 'late'}, cookie=cookie)[0] == 403
    assert not portal.state.history()
    with pytest.raises(ValueError):
        portal.share_text('late')


def test_quotas_and_oversized_body_reject_without_reading(portal, monkeypatch):
    cookie = login(portal)
    monkeypatch.setattr(web, 'MAX_UPLOAD', 3)
    monkeypatch.setattr(web, 'MAX_TOTAL', 4)
    monkeypatch.setattr(web, 'MAX_ITEMS', 2)
    assert request(portal, '/api/file', b'', cookie=cookie, headers={'X-Filename': 'x', 'Content-Length': '4'})[0] == 413
    assert request(portal, '/api/file', b'123', cookie=cookie, headers={'X-Filename': 'x'})[0] == 200
    assert request(portal, '/api/file', b'12', cookie=cookie, headers={'X-Filename': 'x'})[0] == 413
    assert request(portal, '/api/text', {'text': 'a'}, cookie=cookie)[0] == 200
    assert request(portal, '/api/text', {'text': 'b'}, cookie=cookie)[0] == 413
    portal.share_text('a')
    portal.share_text('b')
    with pytest.raises(ValueError):
        portal.share_text('c')


def test_interrupted_upload_cleans_partial_and_refunds_quota(portal):
    cookie = login(portal)
    conn = socket.create_connection(('127.0.0.1', portal.port), timeout=3)
    conn.sendall((f'POST /api/file HTTP/1.1\r\nHost: 127.0.0.1:{portal.port}\r\n'
                  f'Origin: http://127.0.0.1:{portal.port}\r\nX-YayaShare: 1\r\nCookie: {cookie}\r\n'
                  'Content-Length: 1000000\r\nX-Filename: partial.bin\r\n\r\n').encode() + b'partial')
    eventually(lambda: portal.received_count == 1)
    conn.shutdown(socket.SHUT_WR)
    conn.close()
    eventually(lambda: portal.received_count == 0 and not list(portal.state.inbox.iterdir()))
    assert portal.received_bytes == 0 and not portal.state.history()


@pytest.mark.parametrize('headers', [
    {'Content-Length': '-1'}, {'Content-Length': '1, 1'}, {'Transfer-Encoding': 'chunked'},
])
def test_invalid_framing(portal, headers):
    cookie = login(portal)
    assert request(portal, '/api/file', b'', cookie=cookie, headers={'X-Filename': 'x', **headers})[0] == 400
    assert not portal.state.history()
