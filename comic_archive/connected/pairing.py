"""Local bootstrap/approval owner over actual protected state and selected indices.

This is not an authenticated service account, lease, RTC peer or file-read grant.
The service/control/transport owner remains required before any library transfer.
"""
import hashlib
import secrets
import time
import threading
import uuid

from .endpoint import reader_origin
from .files import Index, SourceError
from .jsoncodec import dumps, loads, require
from .signaling import SignalSession, _b64
from .state import HostState
from .wire import _id

CREATE = object()


class PairingDraft:
    def __init__(self, token, pairing_id, session_id, challenge, secret, deadline):
        require(token is CREATE)
        self._pairing_id, self._session_id, self._challenge = pairing_id, session_id, challenge
        self._secret, self._deadline = secret, deadline
        self._approval_id, self._review, self._closed = None, None, False

    pairing_id = property(lambda self: self._pairing_id)
    session_id = property(lambda self: self._session_id)
    challenge = property(lambda self: self._challenge)
    deadline = property(lambda self: self._deadline)
    approval_id = property(lambda self: self._approval_id)
    review = property(lambda self: self._review)
    closed = property(lambda self: self._closed)


class ApprovalReview:
    def __init__(self, token, draft, account_binding, reader_keys, session, digest):
        require(token is CREATE)
        self._draft, self._account_binding = draft, account_binding
        self._reader_json = dumps(reader_keys, 2048)
        self._session, self._digest, self._used = session, digest, False
        self._approval_id = str(uuid.uuid4())

    draft = property(lambda self: self._draft)
    account_binding = property(lambda self: self._account_binding)
    reader_keys = property(lambda self: loads(self._reader_json, 2048))
    session = property(lambda self: self._session)
    digest = property(lambda self: self._digest)
    used = property(lambda self: self._used)
    approval_id = property(lambda self: self._approval_id)


class PairingOwner:
    """One-thread real local owner; pending crypto is not account/lease authority."""

    def __init__(self, state, indices, clock=time.monotonic, wall=time.time):
        require(isinstance(state, HostState) and isinstance(indices, dict) and len(indices) <= 16)
        state.current()
        for share, index in indices.items():
            require(isinstance(index, Index) and index.share_id == share)
            index.root.current()
            require(not state.path.is_relative_to(index.root.path), 'resource_limit')
        self.state, self._indices, self.clock, self.wall = state, dict(indices), clock, wall
        self.drafts, self._reviews, self.closed = {}, {}, False
        # Pending secrets/review objects are intentionally not restored. Record
        # tombstones before accepting another claim after restart/provider switch.
        state.retire_pending()

    def current(self):
        require(threading.get_ident() == self.state.thread, 'cancelled')
        require(not self.closed, 'cancelled')
        try:
            return self.state.current()
        except Exception:
            self.close()
            raise

    def _record(self, pairing_id):
        return self.state.record(pairing_id)

    def _live(self, draft):
        try:
            self.current()
            require(isinstance(draft, PairingDraft) and self.drafts.get(draft.pairing_id) is draft
                    and not draft.closed and self.clock() < draft.deadline)
            record = self._record(draft.pairing_id)
            if draft.approval_id is None:
                return record is not None and record['status'] == 'pending' and record['generation'] == 0
            review = draft.review
            return record is not None and record['status'] == 'approved' and record['generation'] == 0 and review is not None \
                and self._reviews.get(draft.pairing_id) is review and record['approval_id'] == draft.approval_id \
                and record['request_sha256'] == review.digest \
                and record['account_binding'] == review.account_binding and record['reader_keys'] == review.reader_keys
        except Exception:
            return False

    def _retire(self, draft):
        draft._closed, draft._secret = True, None
        if draft.review:
            draft.review.session.close()
        self.drafts.pop(draft.pairing_id, None)
        self._reviews.pop(draft.pairing_id, None)

    def close(self):
        if self.closed:
            return
        # State enforces actual thread custody; no foreign-thread FD retirement.
        require(threading.get_ident() == self.state.thread, 'cancelled')
        self.closed = True
        for draft in list(self.drafts.values()):
            self._retire(draft)
        self.state.close()

    def expire(self):
        self.current()
        for draft in list(self.drafts.values()):
            if self.clock() >= draft.deadline:
                self._retire(draft)
                self.state.revoke(draft.pairing_id)

    def start(self):
        self.expire()
        require(len(self.drafts) < 2, 'busy')
        pairing_id, session_id, challenge = (str(uuid.uuid4()) for _ in range(3))
        draft = PairingDraft(CREATE, pairing_id, session_id, challenge, secrets.token_bytes(32), self.clock() + 300)
        self.state._pending(pairing_id)
        self.drafts[pairing_id] = draft
        return draft

    def qr_url(self, draft, origin):
        require(self._live(draft) and draft._secret is not None, 'revoked')
        reader_origin(origin)
        payload = dict(format='cl1.pairing-qr/1', pairing_id=draft.pairing_id, session_id=draft.session_id,
                       challenge=draft.challenge, host_device_id=self.state.identity.device_id,
                       host_signing_pin=self.state.identity._signing.thumbprint(), bootstrap_secret=_b64(draft._secret))
        # Return to the local UI only. Fragment is never a service body/query/log.
        return origin.rstrip('/') + '/read/connect/#cl1=' + _b64(dumps(payload, 2048).encode('utf-8'))

    def receive(self, draft, account_binding, reader_keys, wire):
        require(self._live(draft) and draft._secret is not None, 'revoked')
        require(draft.review is None, 'busy')
        _id(account_binding)
        session = None
        try:
            bindings = dict(pairing_id=draft.pairing_id, session_id=draft.session_id,
                            challenge=draft.challenge, account_binding=account_binding, generation=0)
            session = SignalSession(self.state.identity, reader_keys, bindings, 'host',
                                    lambda: self._live(draft), clock=self.clock, wall=self.wall)
            claims = session.open(wire, draft._secret)
            require(claims['kind'] == 'pair.request')
            review = ApprovalReview(CREATE, draft, account_binding, claims['body']['reader_keys'], session,
                                    hashlib.sha256(wire.encode('ascii')).hexdigest())
            draft._review = review
            self._reviews[draft.pairing_id] = review
            return review
        except Exception:
            if session:
                session.close()
            self._retire(draft)
            self.state.revoke(draft.pairing_id)
            raise SourceError('integrity_failed') from None

    def describe(self, review):
        require(isinstance(review, ApprovalReview) and self._live(review.draft)
                and review.draft.review is review and self._reviews.get(review.draft.pairing_id) is review and not review.used, 'revoked')
        return loads(dumps(dict(pairing_id=review.draft.pairing_id, account_binding=review.account_binding,
                                reader_keys=review.reader_keys, approval_id=review.approval_id), 4096), 4096)

    def approve(self, review, indices):
        self.describe(review)
        require(isinstance(indices, list) and 1 <= len(indices) <= 16)
        shares = []
        for index in indices:
            require(isinstance(index, Index) and self._indices.get(index.share_id) is index)
            index.root.current()
            shares.append(index.share_id)
        require(len(set(shares)) == len(shares))
        review._used = True  # BEFORE durable write; never rearm uncertain approval.
        draft = review.draft
        try:
            self.state._approve(draft.pairing_id, review.account_binding, review.reader_keys, shares,
                                review.approval_id, review.digest)
            draft._approval_id, draft._secret = review.approval_id, None
            acknowledgement = review.session.seal('pair.accept', dict(shares=shares, generation=0))
            self._retire(draft)  # Bootstrap owner cannot become an active read lease.
            return acknowledgement
        except Exception:
            self._retire(draft)
            if self.state.closed:
                self.close()
            # Committed-but-unacknowledged approval remains stored; no retry
            # mints a second approval. An unusable state owner stays poisoned.
            raise SourceError('integrity_failed') from None

    def revoke(self, pairing_id):
        self.current()
        _id(pairing_id)
        draft = self.drafts.get(pairing_id)
        if draft:
            self._retire(draft)  # Stop local bootstrap first, before disk IO.
        return self.state.revoke(pairing_id)

    def remove_share(self, share_id):
        self.current()
        _id(share_id)
        self._indices.pop(share_id, None)
        return self.state.remove_share(share_id)


    def control_start(self, draft, origin):
        require(self._live(draft) and draft._secret is not None, 'revoked')
        proof = self.state.identity.control_proof(origin, 'POST', '/api/connected-library/v1/pairings',
            dict(pairing_id=draft.pairing_id, session_id=draft.session_id, challenge=draft.challenge), int(self.wall() * 1000))
        require(self._live(draft), 'revoked')
        return proof

    def control_status(self, pairing_id, origin):
        self.current()
        _id(pairing_id)
        require(self._record(pairing_id) is not None, 'not_found')
        proof = self.state.identity.control_proof(origin, 'GET', f'/api/connected-library/v1/pairings/{pairing_id}', {}, int(self.wall() * 1000))
        self.current()
        return proof

    def control_approval(self, pairing_id, origin):
        self.current()
        _id(pairing_id)
        row = self._record(pairing_id)
        require(row is not None and row['status'] == 'approved' and row['generation'] == 0, 'revoked')
        body = dict(pairing_id=pairing_id, account_binding=row['account_binding'], reader_keys=row['reader_keys'],
                    approval_id=row['approval_id'], shares=row['shares'], generation=0)
        proof = self.state.identity.control_proof(origin, 'POST', f'/api/connected-library/v1/pairings/{pairing_id}/approve', body, int(self.wall() * 1000))
        self.current()
        require(self._record(pairing_id) == row, 'revoked')
        return proof

    def control_revocation(self, pairing_id, origin):
        self.current()
        _id(pairing_id)
        row = self._record(pairing_id)
        require(row is not None and row['status'] == 'revoked', 'revoked')
        proof = self.state.identity.control_proof(origin, 'POST', f'/api/connected-library/v1/pairings/{pairing_id}/revoke',
                                                 dict(pairing_id=pairing_id), int(self.wall() * 1000))
        self.current()
        return proof


    def control_socket(self, pairing_id, origin, host_token=None):
        self.current()
        _id(pairing_id)
        row = self._record(pairing_id)
        require(row is not None and row['status'] in {'pending','approved'} and row['generation'] == 0, 'revoked')
        pending = row['status'] == 'pending'
        if pending:
            require(host_token is None)
            draft = self.drafts.get(pairing_id)
            require(draft is not None and self._live(draft), 'revoked')
        else:
            require(isinstance(host_token, str) and len(host_token) == 64 and all(c in '0123456789abcdef' for c in host_token))
        proof = self.state.identity.control_proof(origin, 'GET', '/api/connected-library/v1/signals',
                                                dict(pairing_id=pairing_id, host_token=host_token), int(self.wall() * 1000))
        self.current()
        require(self._record(pairing_id) == row, 'revoked')
        return proof, pending
