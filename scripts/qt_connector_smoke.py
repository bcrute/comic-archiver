"""Check the real Qt HTTPS connector against an isolated synthetic TLS origin."""
import http.server
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comic_archive import browser_egress, qt_engine  # noqa: E402


ORIGIN = 'reader.example.test'
OTHER_NAME = 'different.example.test'
BODY = b'trusted connector fixture'


def make_certs(work):
    def openssl(*args):
        result = subprocess.run(['openssl', *args], cwd=work, timeout=15,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr[-1024:])

    (work / 'fixture.cnf').write_text(
        '[req]\ndistinguished_name=dn\nx509_extensions=ca\n[dn]\n[ca]\n'
        'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n',
        encoding='ascii',
    )
    openssl('req', '-config', 'fixture.cnf', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-days', '1', '-subj', '/CN=Fixture CA', '-keyout', 'ca.key', '-out', 'ca.pem')
    openssl('req', '-config', 'fixture.cnf', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', f'/CN={ORIGIN}',
            '-keyout', 'server.key', '-out', 'server.csr')
    (work / 'server.ext').write_text(
        f'basicConstraints=critical,CA:FALSE\nsubjectAltName=DNS:{ORIGIN}\nextendedKeyUsage=serverAuth\n',
        encoding='ascii')
    openssl('x509', '-req', '-in', 'server.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-CAcreateserial',
            '-extfile', 'server.ext', '-out', 'server.pem', '-days', '1')


class Site(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.headers['Host'], self.path))
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)


def browser_checks(ca):
    from PySide6.QtNetwork import QSslCertificate
    from PySide6 import QtWebEngineCore as core

    def trusted_profile(parent):
        certificates = QSslCertificate.fromData(ca.read_bytes())
        assert len(certificates) == 1
        builder = core.QWebEngineProfileBuilder()
        builder.setAdditionalTrustedCertificates(certificates)
        builder.setPersistentStoragePath(str(ca.parent / 'profile'))
        builder.setCachePath(str(ca.parent / 'cache'))
        profile = builder.createProfile('connector-fixture', parent)
        assert len(profile.additionalTrustedCertificates()) == 1
        return profile

    driver = qt_engine.Driver()
    browser = driver.qt.launch(headless=True)
    try:
        with patch.object(core, 'QWebEngineProfile', side_effect=trusted_profile):
            page = browser.new_context(service_workers='block').new_page()
        assert page.goto(f'https://{ORIGIN}/identity', timeout=12000).status == 200
        assert page.evaluate('document.body.textContent').strip() == BODY.decode()

        def reject(page, name, expected):
            errors = []

            def remember(error):
                errors.append(error.type().name)
                error.rejectCertificate()

            page.loop.call(lambda: page.surface.page.certificateError.connect(remember))
            try:
                page.goto(f'https://{name}/reject', timeout=12000)
            except qt_engine.Error:
                assert expected in errors, errors
            else:
                raise AssertionError('Qt accepted a fixture certificate it should refuse')

        reject(page, OTHER_NAME, 'CertificateCommonNameInvalid')
        untrusted = browser.new_context(service_workers='block').new_page()
        reject(untrusted, ORIGIN, 'CertificateAuthorityInvalid')
    finally:
        browser.close()
        driver.stop()


def main():
    seen, handshakes = [], []
    with tempfile.TemporaryDirectory(prefix='qt-connector-') as name:
        work = Path(name)
        make_certs(work)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(work / 'server.pem', work / 'server.key')
        tls.set_servername_callback(lambda _socket, hostname, _context: handshakes.append(hostname))
        with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Site) as server:
            server.requests = []
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()

            def fixture_connector(host, port, timeout, options):
                seen.append((host, port))
                if host not in {ORIGIN, OTHER_NAME} or port != 443:
                    raise OSError('Outside the synthetic fixture')
                return socket.create_connection(('127.0.0.1', server.server_port), timeout=3)

            try:
                with patch.object(browser_egress, '_connect_public', side_effect=fixture_connector):
                    qt_engine.serve(lambda: browser_checks(work / 'ca.pem'))
                assert (ORIGIN, 443) in seen and (OTHER_NAME, 443) in seen, seen
                assert ORIGIN in handshakes and OTHER_NAME in handshakes, handshakes
                assert [row for row in server.requests if row[1] != '/favicon.ico'] == [(ORIGIN, '/identity')], server.requests
            finally:
                server.shutdown()
                worker.join(timeout=3)
    print('Qt connector preserved TLS identity and refused the untrusted CA and wrong name', flush=True)


if __name__ == '__main__':
    main()
