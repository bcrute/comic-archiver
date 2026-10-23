"""Real JOSE/key operations when selected on fleet; fake policy/clocks only.

Default installs may skip the opt-in extra. CI-READER-CONNECTED MUST install
.[connected,test], refuse absent crypto and run these, never count a skip as pass.
No accepted peer/account/share/lease/approval fixture is minted from this DATA.
"""
import base64
import hashlib
import importlib.util
import json
import unittest
import uuid

from comic_archive.connected.files import SourceError

AVAILABLE = importlib.util.find_spec('jwcrypto') is not None
if AVAILABLE:
    from comic_archive.connected.signaling import Identity, SignalSession, _encrypted_header, verify_bundle_proof
else:
    Identity = SignalSession = _encrypted_header = verify_bundle_proof = None


def identifier():
    return str(uuid.uuid4())


def offer(text='v=0\r\na=fingerprint:sha-256 SYNTHETIC-SDP-BYTES\r\n'):
    return dict(sdp=text, sha256=hashlib.sha256(text.encode('utf-8')).hexdigest())


@unittest.skipUnless(AVAILABLE, 'Opt-in connected extra required; skip is not connected qualification')
class ConnectedSignalTests(unittest.TestCase):
    def setUp(self):
        self.reader, self.host = Identity.generate(), Identity.generate()
        self.bindings = dict(pairing_id=identifier(), session_id=identifier(), account_binding=identifier(),
                             challenge=identifier(), generation=0)
        self.now, self.wall, self.allowed = 0, 1_800_000_000, True
        self.r, self.h = self.session('reader'), self.session('host')

    def session(self, role, **changes):
        return SignalSession(self.host if role == 'host' else self.reader,
                             (self.reader if role == 'host' else self.host).public_bundle(),
                             dict(self.bindings, **changes), role, lambda: self.allowed,
                             clock=lambda: self.now, wall=lambda: self.wall)

    def test_actual_keys_expose_only_public_separate_bundles_and_do_not_alias_dto(self):
        bundle = self.reader.public_bundle()
        self.assertEqual(set(bundle['signing']), {'kty', 'crv', 'x', 'y'})
        self.assertNotEqual(bundle['signing'], bundle['exchange'])
        bundle['signing']['x'] = 'changed external DTO'
        self.assertNotEqual(bundle, self.reader.public_bundle())
        with self.assertRaises(SourceError):
            Identity(None, identifier(), {}, {})

    def test_qr_signing_pin_binds_exchange_key_before_initial_encryption(self):
        challenge, now = identifier(), 1_800_000_000_000
        pin = self.host._signing.thumbprint()
        proof = self.host.attest(challenge, 'host', now)
        self.assertEqual(verify_bundle_proof(proof, pin, challenge, 'host', now), self.host.public_bundle())
        parts = proof.split('.')
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        payload['bundle']['exchange'] = self.reader.public_bundle()['exchange']
        parts[1] = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b'=').decode()
        with self.assertRaises(SourceError):
            verify_bundle_proof('.'.join(parts), pin, challenge, 'host', now)
        for current, role, stamp in [(identifier(), 'host', now), (challenge, 'reader', now), (challenge, 'host', now + 20_000)]:
            with self.assertRaises(SourceError):
                verify_bundle_proof(proof, pin, current, role, stamp)

    def test_actual_bootstrap_encryption_signature_and_host_ack_context(self):
        secret = b'*' * 32
        wire = self.r.seal('pair.request', dict(reader_keys=self.reader.public_bundle()), secret)
        self.assertEqual(len(wire.split('.')), 5)
        self.assertNotIn(self.bindings['account_binding'], wire)
        value = self.h.open(wire, secret)
        self.assertEqual(value['body']['reader_keys'], self.reader.public_bundle())
        shares = [identifier()]
        response = self.h.seal('pair.accept', dict(shares=shares, generation=0))
        self.assertEqual(self.r.open(response)['body'], dict(shares=shares, generation=0))
        # This is actual crypto DATA, not proof of local approval/account/lease.

    def test_exact_sdp_bytes_bind_to_signed_digest_and_reverse_answer(self):
        body = offer()
        wire = self.r.seal('offer', body)
        self.assertNotIn('SYNTHETIC-SDP', wire)
        self.assertEqual(self.h.open(wire)['body'], body)
        response = self.h.seal('answer', offer(body['sdp'] + 'a=setup:active\r\n'))
        self.assertEqual(self.r.open(response)['kind'], 'answer')
        with self.assertRaises(SourceError):
            self.session('reader').seal('offer', dict(body, sdp=body['sdp'].replace('\r\n', '\n')))

    def test_wrong_bootstrap_and_role_or_secret_on_ordinary_traffic_refuse(self):
        wire = self.r.seal('pair.request', dict(reader_keys=self.reader.public_bundle()), b'*' * 32)
        with self.assertRaises(SourceError):
            self.h.open(wire, b'+' * 32)
        with self.assertRaises(SourceError):
            self.h.open(wire, b'*' * 32)
        with self.assertRaises(SourceError):
            self.session('host').seal('offer', offer())
        with self.assertRaises(SourceError):
            self.session('reader').seal('offer', offer(), b'*' * 32)

    def test_foreign_account_pair_session_challenge_generation_and_replay_refuse(self):
        wire = self.r.seal('offer', offer())
        for field in ['account_binding', 'pairing_id', 'session_id', 'challenge', 'generation']:
            with self.subTest(field=field), self.assertRaises(SourceError):
                self.session('host', **{field:1 if field == 'generation' else identifier()}).open(wire)
        self.assertEqual(self.h.open(wire)['kind'], 'offer')
        with self.assertRaises(SourceError):
            self.h.open(wire)

    def test_expiry_uses_monotonic_elapsed_not_rolled_back_wall_and_policy_rechecks(self):
        wire = self.r.seal('offer', offer())
        self.now, self.wall = 20, 1
        with self.assertRaises(SourceError):
            self.h.open(wire)
        self.now, self.wall = 0, 1_800_000_000
        other = self.session('host')
        self.allowed = False
        with self.assertRaises(SourceError):
            other.open(wire)

    def test_tamper_duplicate_header_private_or_reused_key_and_curve_refuse(self):
        wire = self.r.seal('offer', offer())
        parts = wire.split('.')
        parts[-1] = ('A' if parts[-1][0] != 'A' else 'B') + parts[-1][1:]
        with self.assertRaises(SourceError):
            self.h.open('.'.join(parts))
        parts = wire.split('.')
        parts[0] = base64.urlsafe_b64encode(b'{"alg":"ECDH-ES","alg":"ECDH-ES"}').rstrip(b'=').decode()
        with self.assertRaises(SourceError):
            _encrypted_header('.'.join(parts), self.host._exchange.thumbprint())
        for changed in [dict(self.host.public_bundle(), exchange=self.host.public_bundle()['signing']),
                        dict(self.host.public_bundle(), signing=dict(self.host.public_bundle()['signing'], d='private')),
                        dict(self.host.public_bundle(), exchange=dict(self.host.public_bundle()['exchange'], crv='P-384'))]:
            with self.assertRaises(SourceError):
                SignalSession(self.reader, changed, self.bindings, 'reader', lambda: True)

    def test_rate_and_candidate_budgets_do_not_silently_reset(self):
        candidate = dict(candidate='candidate:synthetic-only', sdpMid='0', sdpMLineIndex=0)
        for _ in range(10):
            self.r.seal('candidate', candidate)
        with self.assertRaises(SourceError):
            self.r.seal('candidate', candidate)
        other = self.session('reader')
        for _ in range(64):
            self.now += 1.001
            self.h.open(other.seal('candidate', candidate))
        self.now += 1.001
        with self.assertRaises(SourceError):
            other.seal('candidate', candidate)
