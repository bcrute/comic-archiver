"""CL-1 endpoint JOSE, never account/consent/lease or accepted-peer authority.

Uses optional jwcrypto, imports start no listener. The future pairing owner must
pin verified bundles, hold actual local approval/current leases and serialize
calls. Returned verified claims are DATA, not a read grant or Runtime owner.
"""
import base64
from collections import deque
import hashlib
import hmac
import re
import time
import uuid
from types import MappingProxyType

from jwcrypto import jwk, jwe, jws

from .endpoint import reader_origin
from .files import SourceError
from .jsoncodec import dumps, loads, require
from .wire import _id

WIRE_MAX = 192 * 1024
INNER_MAX = 144 * 1024
CLAIMS_MAX = 96 * 1024
PROFILE = 'cl1.signal/1'
JWS_TYPE = 'cl1-signal+jws'
BUNDLE_TYPE = 'cl1-bundle+jws'
CREATE = object()
KINDS = {'pair.request', 'pair.accept', 'offer', 'answer', 'candidate', 'restart'}
BINDINGS = {'pairing_id', 'session_id', 'account_binding', 'challenge', 'generation'}
KEYS = {'kty', 'crv', 'x', 'y'}


def _integer(value, low, high):
    require(type(value) is int and low <= value <= high)


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _unb64(value, maximum):
    require(isinstance(value, str) and len(value) <= (maximum * 4 + 2) // 3
            and re.fullmatch(r'[A-Za-z0-9_-]*', value) is not None)
    try:
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        require(len(raw) <= maximum and _b64(raw) == value)
        return raw
    except ValueError:
        raise SourceError('integrity_failed') from None


def _public(value):
    try:
        require(isinstance(value, dict) and set(value) == KEYS and value['kty'] == 'EC' and value['crv'] == 'P-256')
        require(len(_unb64(value['x'], 32)) == 32 and len(_unb64(value['y'], 32)) == 32)
        key = jwk.JWK(**value)
        key.get_op_key('verify')  # Library validates the actual P-256 point.
        return key
    except Exception:
        raise SourceError('integrity_failed') from None


def _bundle(value):
    require(isinstance(value, dict) and set(value) == {'device_id', 'signing', 'exchange'})
    _id(value['device_id'])
    signing, exchange = _public(value['signing']), _public(value['exchange'])
    require(signing.thumbprint() != exchange.thumbprint())
    return signing, exchange


def _header(compact, count):
    require(isinstance(compact, str) and 0 < len(compact) <= WIRE_MAX and compact.isascii())
    parts = compact.split('.')
    require(len(parts) == count)
    header = loads(_unb64(parts[0], 2048).decode('utf-8'), 2048)
    require(isinstance(header, dict))
    return parts, header


def _signed_header(compact, thumb, kind=JWS_TYPE):
    parts, header = _header(compact, 3)
    require(header == {'alg': 'ES256', 'typ': kind, 'kid': thumb})
    require(len(_unb64(parts[2], 64)) == 64)
    _unb64(parts[1], CLAIMS_MAX)


def _encrypted_header(compact, thumb):
    parts, header = _header(compact, 5)
    require(set(header) == {'alg', 'enc', 'cty', 'kid', 'epk'} and header['alg'] == 'ECDH-ES'
            and header['enc'] == 'A256GCM' and header['cty'] == JWS_TYPE and header['kid'] == thumb)
    _public(header['epk'])
    require(parts[1] == '' and len(_unb64(parts[2], 12)) == 12 and len(_unb64(parts[4], 16)) == 16)
    _unb64(parts[3], INNER_MAX)


def _body(kind, value, bundle):
    require(isinstance(value, dict))
    if kind in {'offer', 'answer'}:
        require(set(value) == {'sdp', 'sha256'} and isinstance(value['sdp'], str)
                and 0 < len(value['sdp'].encode('utf-8')) <= 65536
                and value['sha256'] == hashlib.sha256(value['sdp'].encode('utf-8')).hexdigest())
    elif kind == 'candidate':
        require(set(value) == {'candidate', 'sdpMid', 'sdpMLineIndex'} and isinstance(value['candidate'], str)
                and len(value['candidate'].encode('utf-8')) <= 8192)
        require(value['sdpMid'] is None or isinstance(value['sdpMid'], str) and len(value['sdpMid'].encode('utf-8')) <= 64)
        if value['sdpMLineIndex'] is not None:
            _integer(value['sdpMLineIndex'], 0, 65535)
    elif kind == 'pair.request':
        require(set(value) == {'reader_keys'} and value['reader_keys'] == bundle)
    elif kind == 'pair.accept':
        require(set(value) == {'shares', 'generation'} and isinstance(value['shares'], list) and len(value['shares']) <= 16)
        for share in value['shares']:
            _id(share)
        require(len(set(value['shares'])) == len(value['shares']))
        _integer(value['generation'], 0, 2**31 - 1)
    elif kind == 'restart':
        require(set(value) == {'session_id', 'challenge'})
        _id(value['session_id'])
        _id(value['challenge'])


class Identity:
    """Owns actual library-generated private keys; public bundles grant no access.

    Persistence/reset/OS storage is NOT implemented by a bundle import. No public
    constructor restores private ownership from an untrusted DTO or index row.
    """

    def __init__(self, token, device_id, signing, exchange):
        require(token is CREATE)
        self._device_id, self._signing, self._exchange = device_id, signing, exchange
        self._public = dict(device_id=device_id, signing=signing.export_public(as_dict=True),
                            exchange=exchange.export_public(as_dict=True))
        _bundle(self._public)

    device_id = property(lambda self: self._device_id)

    @classmethod
    def generate(cls):
        return cls(CREATE, str(uuid.uuid4()), jwk.JWK.generate(kty='EC', crv='P-256'),
                   jwk.JWK.generate(kty='EC', crv='P-256'))

    def public_bundle(self):
        return loads(dumps(self._public, 2048), 2048)

    def control_proof(self, origin, method, path, body, now_ms):
        """Sign public HTTP control metadata; never account or read authority.

        Protected-state/local-approval callers must supply their actual current
        ownership. No token, cookie, provider credential or comic body belongs here.
        """
        try:
            reader_origin(origin)
            require(method in {'GET', 'POST', 'DELETE'} and isinstance(path, str) and path.startswith('/api/connected-library/v1/')
                    and '?' not in path and '#' not in path and len(path) <= 256 and isinstance(body, dict))
            _integer(now_ms, 0, 2**53 - 1 - 20_000)
            claims = dict(v=1, protocol='cl1.control/1', request_id=str(uuid.uuid4()), issued_at=now_ms,
                          expires_at=now_ms + 20_000, origin=origin, method=method, path=path,
                          host_keys=self.public_bundle(), body=body)
            signed = jws.JWS(dumps(claims, 4096).encode('utf-8'))
            signed.allowed_algs = ['ES256']
            signed.add_signature(self._signing, alg='ES256',
                                 protected={'alg':'ES256', 'typ':'cl1-control+jws', 'kid':self._signing.thumbprint()})
            result = signed.serialize(compact=True)
            require(len(result) <= 8192)
            return result
        except Exception:
            raise SourceError('integrity_failed') from None

    def attest(self, challenge, role, now_ms):
        try:
            _id(challenge)
            require(role in {'host', 'reader'})
            _integer(now_ms, 0, 2**53 - 1 - 20_000)
            claims = dict(v=1, protocol='cl1.bundle/1', role=role, challenge=challenge,
                          issued_at=now_ms, expires_at=now_ms + 20_000, bundle=self.public_bundle())
            signed = jws.JWS(dumps(claims, 4096).encode('utf-8'))
            signed.allowed_algs = ['ES256']
            signed.add_signature(self._signing, alg='ES256',
                                 protected={'alg':'ES256', 'typ':BUNDLE_TYPE, 'kid':self._signing.thumbprint()})
            return signed.serialize(compact=True)
        except Exception:
            raise SourceError('integrity_failed') from None


def verify_bundle_proof(wire, signing_pin, challenge, role, now_ms):
    """Verify QR-pinned binding BEFORE encrypting to a service-supplied host key.

    Returns key-possession DATA, not account approval or an authorized RTC peer.
    The pairing owner must supply the actual QR pin and fresh expected challenge.
    """
    try:
        require(isinstance(wire, str) and len(wire) <= 8192 and len(_unb64(signing_pin, 32)) == 32)
        _id(challenge)
        require(role in {'host', 'reader'})
        _integer(now_ms, 0, 2**53 - 1)
        _signed_header(wire, signing_pin, BUNDLE_TYPE)
        claims = loads(_unb64(wire.split('.')[1], 4096).decode('utf-8'), 4096)
        require(isinstance(claims, dict) and set(claims) == {'v', 'protocol', 'role', 'challenge', 'issued_at', 'expires_at', 'bundle'}
                and type(claims['v']) is int and claims['v'] == 1 and claims['protocol'] == 'cl1.bundle/1'
                and claims['role'] == role and claims['challenge'] == challenge)
        signing, _ = _bundle(claims['bundle'])
        require(signing.thumbprint() == signing_pin)
        signed = jws.JWS()
        signed.allowed_algs = ['ES256']
        signed.deserialize(wire)
        signed.verify(signing, alg='ES256')
        _integer(claims['issued_at'], 0, 2**53 - 1)
        _integer(claims['expires_at'], 0, 2**53 - 1)
        require(claims['issued_at'] <= now_ms + 5000 and claims['issued_at'] <= claims['expires_at']
                and now_ms < claims['expires_at'] <= claims['issued_at'] + 20_000)
        return claims['bundle']
    except Exception:
        raise SourceError('integrity_failed') from None


def _identity_record(identity):
    """Private filesystem serialization only; never a wire/public bundle."""
    require(isinstance(identity, Identity))
    return dict(format='cl1.host-identity/1', device_id=identity.device_id,
                signing=identity._signing.export_private(as_dict=True),
                exchange=identity._exchange.export_private(as_dict=True))


def _restore_identity(value):
    """Import actual private material read by the protected store owner.

    This proves key possession only, never a pairing/account/read grant.
    """
    try:
        require(isinstance(value, dict) and set(value) == {'format', 'device_id', 'signing', 'exchange'}
                and value['format'] == 'cl1.host-identity/1')
        _id(value['device_id'])
        restored = []
        for name in ['signing', 'exchange']:
            record = value[name]
            require(isinstance(record, dict) and set(record) == KEYS | {'d'})
            _public({key: record[key] for key in KEYS})
            require(len(_unb64(record['d'], 32)) == 32)
            key = jwk.JWK(**record)
            key.get_op_key('sign')  # Validates actual private/public point consistency.
            restored.append(key)
        return Identity(CREATE, value['device_id'], *restored)
    except Exception:
        raise SourceError('integrity_failed') from None


class SignalSession:
    """Encrypted, signed and replay-bounded context; NOT an authorized RTC peer.

    live() is required to recheck the future actual owner. It is an integration
    callback, not proof of account binding, local approval or an active lease.
    One owner serializes send/open and destroys this context on any uncertainty.
    """

    def __init__(self, identity, peer_bundle, bindings, role, live, clock=time.monotonic, wall=time.time):
        require(isinstance(identity, Identity) and role in {'host', 'reader'} and callable(live))
        require(isinstance(bindings, dict) and set(bindings) == BINDINGS)
        for key in BINDINGS - {'generation'}:
            _id(bindings[key])
        _integer(bindings['generation'], 0, 2**31 - 1)
        self.identity, self.role, self.live, self.clock = identity, role, live, clock
        self.peer_bundle = loads(dumps(peer_bundle, 2048), 2048)
        self.peer_signing, self.peer_exchange = _bundle(self.peer_bundle)
        require(identity.device_id != self.peer_bundle['device_id'])
        self.bindings = MappingProxyType(dict(bindings))
        self.started, self.wall_ms = clock(), int(wall() * 1000)
        self.sent, self.received, self.sent_candidates, self.received_candidates = 0, 0, 0, 0
        self.closed = False
        self.tx_events, self.rx_events = deque(), deque()
        self._current()

    def close(self):
        self.closed = True

    def _current(self):
        require(not self.closed, 'cancelled')
        try:
            require(self.live() is True, 'revoked')
        except Exception:
            self.close()
            raise SourceError('revoked') from None

    def _now(self):
        elapsed = self.clock() - self.started
        require(elapsed >= 0)
        return self.wall_ms + int(elapsed * 1000)

    def _rate(self, events):
        now = self.clock()
        while events and now - events[0] >= 1:
            events.popleft()
        require(len(events) < 10, 'resource_limit')
        events.append(now)

    def _claims(self, value, outbound):
        fields = {'v', 'protocol', 'kind', *BINDINGS, 'from_device', 'to_device', 'from_signing',
                  'to_signing', 'from_exchange', 'to_exchange', 'sequence', 'issued_at', 'expires_at', 'body'}
        require(isinstance(value, dict) and set(value) == fields and value['v'] == 1
                and type(value['v']) is int and value['protocol'] == PROFILE and value['kind'] in KINDS)
        _integer(value['generation'], 0, 2**31 - 1)
        require(all(value[key] == self.bindings[key] for key in BINDINGS))
        source = self.identity.public_bundle() if outbound else self.peer_bundle
        target = self.peer_bundle if outbound else self.identity.public_bundle()
        source_sign, source_exchange = _bundle(source)
        target_sign, target_exchange = _bundle(target)
        require(value['from_device'] == source['device_id'] and value['to_device'] == target['device_id']
                and value['from_signing'] == source_sign.thumbprint() and value['to_signing'] == target_sign.thumbprint()
                and value['from_exchange'] == source_exchange.thumbprint() and value['to_exchange'] == target_exchange.thumbprint())
        sender_role = self.role if outbound else ('host' if self.role == 'reader' else 'reader')
        if value['kind'] in {'pair.request', 'offer'}:
            require(sender_role == 'reader')
        if value['kind'] in {'pair.accept', 'answer'}:
            require(sender_role == 'host')
        expected = self.sent if outbound else self.received
        require(type(value['sequence']) is int and value['sequence'] == expected and expected < 2**31)
        _integer(value['issued_at'], 0, 2**53 - 1)
        _integer(value['expires_at'], 0, 2**53 - 1)
        now = self._now()
        require(value['issued_at'] <= now + 5000 and value['issued_at'] <= value['expires_at']
                and now < value['expires_at'] <= value['issued_at'] + 20_000)
        _body(value['kind'], value['body'], source)
        if value['kind'] == 'pair.accept':
            require(value['body']['generation'] == self.bindings['generation'])
        if value['kind'] == 'candidate':
            require((self.sent_candidates if outbound else self.received_candidates) < 64, 'resource_limit')
        if value['kind'] == 'restart':
            require(value['body']['session_id'] != self.bindings['session_id'] and value['body']['challenge'] != self.bindings['challenge'])

    def seal(self, kind, body, bootstrap_secret=None):
        try:
            self._current()
            self._rate(self.tx_events)
            mine = self.identity.public_bundle()
            now = self._now()
            value = dict(v=1, protocol=PROFILE, kind=kind, **self.bindings,
                         from_device=mine['device_id'], to_device=self.peer_bundle['device_id'],
                         from_signing=self.identity._signing.thumbprint(), to_signing=self.peer_signing.thumbprint(),
                         from_exchange=self.identity._exchange.thumbprint(), to_exchange=self.peer_exchange.thumbprint(),
                         sequence=self.sent, issued_at=now, expires_at=now + 20_000,
                         body=loads(dumps(body, CLAIMS_MAX), CLAIMS_MAX))
            self._claims(value, True)
            self.sent += 1  # Consume before signing; never repeat uncertain sends.
            if kind == 'candidate':
                self.sent_candidates += 1
            signed = jws.JWS(dumps(value, CLAIMS_MAX).encode('utf-8'))
            signed.allowed_algs = ['ES256']
            signed.add_signature(self.identity._signing, alg='ES256',
                                 protected={'alg':'ES256', 'typ':JWS_TYPE, 'kid':self.identity._signing.thumbprint()})
            inner = signed.serialize(compact=True)
            require((kind == 'pair.request') == (bootstrap_secret is not None))
            proof = None
            if bootstrap_secret is not None:
                require(isinstance(bootstrap_secret, bytes) and len(bootstrap_secret) == 32)
                proof = _b64(hmac.digest(bootstrap_secret, b'CL1/pair/1\n' + hashlib.sha256(inner.encode('ascii')).digest(), 'sha256'))
            encrypted = jwe.JWE(dumps(dict(inner_jws=inner, bootstrap_proof=proof), INNER_MAX).encode('utf-8'),
                                protected={'alg':'ECDH-ES', 'enc':'A256GCM', 'cty':JWS_TYPE, 'kid':self.peer_exchange.thumbprint()},
                                algs=['ECDH-ES', 'A256GCM'], recipient=self.peer_exchange)
            wire = encrypted.serialize(compact=True)
            _encrypted_header(wire, self.peer_exchange.thumbprint())
            self._current()
            return wire
        except Exception:
            self.close()
            raise SourceError('integrity_failed') from None

    def open(self, wire, bootstrap_secret=None):
        try:
            self._current()
            self._rate(self.rx_events)
            _encrypted_header(wire, self.identity._exchange.thumbprint())
            encrypted = jwe.JWE(algs=['ECDH-ES', 'A256GCM'])
            encrypted.deserialize(wire)
            encrypted.decrypt(self.identity._exchange, max_plaintext=INNER_MAX)
            wrapper = loads(encrypted.payload.decode('utf-8'), INNER_MAX)
            require(isinstance(wrapper, dict) and set(wrapper) == {'inner_jws', 'bootstrap_proof'})
            inner = wrapper['inner_jws']
            _signed_header(inner, self.peer_signing.thumbprint())
            signed = jws.JWS()
            signed.allowed_algs = ['ES256']
            signed.deserialize(inner)
            signed.verify(self.peer_signing, alg='ES256')
            value = loads(signed.payload.decode('utf-8'), CLAIMS_MAX)
            self._claims(value, False)
            require((value['kind'] == 'pair.request') == (bootstrap_secret is not None))
            if bootstrap_secret is None:
                require(wrapper['bootstrap_proof'] is None)
            else:
                require(isinstance(bootstrap_secret, bytes) and len(bootstrap_secret) == 32)
                supplied = _unb64(wrapper['bootstrap_proof'], 32)
                expected = hmac.digest(bootstrap_secret, b'CL1/pair/1\n' + hashlib.sha256(inner.encode('ascii')).digest(), 'sha256')
                require(hmac.compare_digest(supplied, expected))
            self._current()
            self.received += 1
            if value['kind'] == 'candidate':
                self.received_candidates += 1
            return value  # DATA only, not local approval or active read authority.
        except Exception:
            self.close()
            raise SourceError('integrity_failed') from None
