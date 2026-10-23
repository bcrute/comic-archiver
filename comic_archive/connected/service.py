"""Dedicated actual host HTTP client. No site/provider/web-account session reuse.

Replies are metadata/acknowledgements only, never Account/RTC/Lease authority.
No transport injection, automatic retry/redirect, cookie persistence or logging.
"""
import asyncio
import re

import httpx

from .endpoint import reader_origin as canonical_origin
from .files import SourceError
from .jsoncodec import loads, require
from .pairing import PairingOwner
from .signaling import _bundle
from .wire import _id

PREFIX = '/api/connected-library/v1'


def _same_status(value, pairing_id):
    require(isinstance(value, dict) and value.get('pairing_id') == pairing_id)
    if value.get('status') == 'revoked':
        require(set(value) == {'pairing_id','status'})
        return value
    require(set(value) == {'pairing_id','session_id','challenge','status','expires_at','account_binding',
                           'reader_keys','approval_id','generation','shares','account_label'})
    for field in ['session_id','challenge']:
        _id(value[field])
    require(value['status'] in {'pending','claimed','approved','expired'} and type(value['generation']) is int
            and 0 <= value['generation'] < 2**31 and type(value['expires_at']) is int
            and isinstance(value['shares'], list) and len(value['shares']) <= 16)
    for share in value['shares']:
        _id(share)
    require(len(set(value['shares'])) == len(value['shares']))
    if value['account_binding'] is not None:
        _id(value['account_binding'])
        _bundle(value['reader_keys'])
        require(isinstance(value['account_label'], str) and len(value['account_label'].encode('utf-8')) <= 600)
    else:
        require(value['reader_keys'] is None and value['account_label'] is None)
    if value['approval_id'] is not None:
        _id(value['approval_id'])
    return value


class HostService:
    def __init__(self, owner, reader_origin, preview_token=None):
        require(isinstance(owner, PairingOwner))
        owner.current()
        self.owner, self.origin = owner, canonical_origin(reader_origin)
        if preview_token is not None:
            require(isinstance(preview_token, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,256}', preview_token))
        self._preview, self._closed, self._lock = preview_token, False, asyncio.Lock()
        self._closing = None
        # A private, ordinary maintained client; no downloader/site transport,
        # environment proxy, auth jar, provider/browser worker or injected fake.
        self._client = httpx.AsyncClient(follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(5, connect=5, write=2, pool=2),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1))

    def current(self):
        require(not self._closed, 'cancelled')
        self.owner.current()

    async def _request(self, method, path, proof):
        self.current()
        require(method in {'GET','POST'} and path.startswith(PREFIX + '/') and '?' not in path and '#' not in path)
        try:
            await asyncio.wait_for(self._lock.acquire(), 2)
        except TimeoutError:
            raise SourceError('busy') from None
        try:
            self.current()
            headers = {'Authorization':'CL1-Proof ' + proof, 'Accept':'application/json', 'Accept-Encoding':'identity'}
            if self._preview is not None:
                headers['Cookie'] = 'ink_preview=' + self._preview
            self._client.cookies.clear()
            async def receive():
                async with self._client.stream(method, self.origin + path, headers=headers) as response:
                    require(not response.headers.get_list('set-cookie'), 'integrity_failed')
                    require(response.headers.get('content-encoding', 'identity') == 'identity', 'integrity_failed')
                    require(response.status_code < 300 or response.status_code >= 400, 'connection_unavailable')
                    require(response.headers.get('content-type', '').split(';')[0].strip() == 'application/json', 'connection_unavailable')
                    data = bytearray()
                    async for chunk in response.aiter_raw():
                        require(len(data) + len(chunk) <= 8192, 'resource_limit')
                        data.extend(chunk)
                    value = loads(data.decode('utf-8'), 8192)
                    if response.status_code != 200:
                        # HTTP/network ambiguity never becomes a permanent local
                        # revoke, a second grant or an exception containing URLs.
                        codes = {403:'auth_required',401:'auth_required',404:'not_found',429:'busy',503:'connection_unavailable'}
                        raise SourceError(codes.get(response.status_code, 'integrity_failed'))
                    require(isinstance(value, dict))
                    return value
            value = await asyncio.wait_for(receive(), 8)
            self.current()
            return value
        except (httpx.HTTPError, TimeoutError, UnicodeError):
            raise SourceError('connection_unavailable') from None
        finally:
            self._client.cookies.clear()
            self._lock.release()

    async def register(self, draft):
        self.current()
        proof = self.owner.control_start(draft, self.origin)
        value = await self._request('POST', PREFIX + '/pairings', proof)
        require(self.owner._live(draft), 'revoked')
        require(set(value) == {'pairing_id','host_device_id','session_id','challenge','status','generation','expires_at'}
                and value['pairing_id'] == draft.pairing_id and value['host_device_id'] == self.owner.state.identity.device_id
                and value['session_id'] == draft.session_id and value['challenge'] == draft.challenge
                and value['status'] in {'pending','claimed'} and type(value['generation']) is int and value['generation'] == 0
                and type(value['expires_at']) is int and int(self.owner.wall() * 1000) < value['expires_at'] <= int(self.owner.wall() * 1000) + 305000)
        return value

    async def status(self, pairing_id):
        self.current()
        _id(pairing_id)
        value = await self._request('GET', PREFIX + '/pairings/' + pairing_id, self.owner.control_status(pairing_id, self.origin))
        return _same_status(value, pairing_id)

    async def acknowledge(self, pairing_id):
        """Reconcile ONE already durable local approval; never approve again."""
        self.current()
        before = self.owner._record(pairing_id)
        require(before is not None and before['status'] == 'approved', 'revoked')
        operation = 'approve' if before['generation'] == 0 else 'scope'
        proof = (self.owner.control_approval if operation == 'approve' else self.owner.control_scope)(pairing_id, self.origin)
        value = await self._request('POST', PREFIX + '/pairings/' + pairing_id + '/' + operation, proof)
        self.current()
        require(self.owner._record(pairing_id) == before, 'revoked')
        require(set(value) == {'pairing_id','approval_id','generation','shares','host_token','token_expires_at'}
                and value['pairing_id'] == pairing_id and value['approval_id'] == before['approval_id']
                and type(value['generation']) is int and value['generation'] == before['generation']
                and value['shares'] == before['shares'] and isinstance(value['host_token'], str)
                and re.fullmatch(r'[0-9a-f]{64}', value['host_token']) and type(value['token_expires_at']) is int
                and int(self.owner.wall() * 1000) < value['token_expires_at'] <= int(self.owner.wall() * 1000) + 2592005000)
        # DATA over an actual authenticated HTTP exchange. Only the real WSS
        # admission factory consumes this token; it grants no web/file session.
        return value

    async def revoke(self, pairing_id):
        self.current()
        proof = self.owner.control_revocation(pairing_id, self.origin)
        value = await self._request('POST', PREFIX + '/pairings/' + pairing_id + '/revoke', proof)
        require(value == {'pairing_id':pairing_id,'status':'revoked'})
        return value

    async def close(self):
        self._closed = True
        # A racing in-flight request loses current() before it can acknowledge.
        if self._closing is None:
            self._closing = asyncio.create_task(self._client.aclose())
        done, _ = await asyncio.wait({self._closing}, timeout=3)
        require(self._closing in done, 'connection_unavailable')
        try:
            self._closing.result()
        except (httpx.HTTPError, OSError):
            raise SourceError('connection_unavailable') from None
