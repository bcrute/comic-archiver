"""One-thread host pairing/signaling controller over actual native owners.

No local HTTP listener or downloader/site session. Actual RTC/file-worker owners
combine native accepted peers and local scope with the service deadline.
"""

import asyncio
import time

from .files import SourceError
from .jsoncodec import dumps, loads, require
from .pairing import ApprovalReview, PairingOwner
from .rendezvous import HostRendezvous, UnclosedSocket
from .service import HostService
from .signaling import SignalSession
from .wire import _id

_CREATE = object()


class PendingPairing:
    def __init__(self, token, draft):
        require(token is _CREATE)
        self._draft = draft
        self._phase, self._review, self._label = "registering", None, None
        self._ack, self._ack_attempted = None, False
        self._changed, self._approval_lock = asyncio.Event(), asyncio.Lock()

    draft = property(lambda self: self._draft)
    phase = property(lambda self: self._phase)
    review = property(lambda self: self._review)
    account_label = property(lambda self: self._label)

    def _set(self, phase):
        self._phase = phase
        self._changed.set()

    async def wait_review(self):
        while self._review is None:
            self._changed.clear()
            require(
                self._phase in {"registering", "registration_unknown", "waiting"},
                "connection_unavailable",
            )
            await self._changed.wait()
        return self._review  # Actual registered local Review, never JSON consent.


class _Runner:
    def __init__(self, controller, transport, pending=None):
        require(isinstance(transport, HostRendezvous))
        self.controller, self.transport, self.pending = controller, transport, pending
        self.pairing_id = transport.pairing_id
        self.task, self.receiving, self.crypto = None, None, None
        self.peer = None
        self.stopping, self.joined, self.failure = False, False, None
        self.signals = asyncio.Queue(maxsize=8)

    def stop(self):
        self.stopping = True
        if self.peer is not None:
            self.peer.invalidate()
        self.transport.invalidate()  # Synchronous permission retirement first.
        if self.crypto is not None:
            self.crypto.close()

    def current(self):
        if self.stopping or self.controller.closed:
            return False
        try:
            self.transport._current()
            return True
        except SourceError:
            return False

    async def _event(self, value):
        kind = value["type"]
        if kind == "peer":
            bound = self.transport.binding
            status = await self.controller.service.status(self.pairing_id)
            require(self.current(), "revoked")
            require(
                status["status"] in {"claimed", "approved"}
                and status["account_binding"] == bound["account_binding"]
                and status["reader_keys"] == bound["reader_keys"]
                and status["generation"] == bound["generation"]
            )
            if self.pending is not None:
                require(
                    status["status"] == "claimed"
                    and status["session_id"] == self.pending.draft.session_id
                    and status["challenge"] == self.pending.draft.challenge
                    and bound["pending"]
                )
                self.pending._label = status["account_label"]
            else:
                row = self.controller.owner._record(self.pairing_id)
                require(
                    row is not None
                    and row["status"] == "approved"
                    and row["generation"] == bound["generation"]
                    and row["account_binding"] == bound["account_binding"]
                    and row["reader_keys"] == bound["reader_keys"]
                    and row["shares"] == bound["shares"]
                    and status["approval_id"] == row["approval_id"]
                )
                scope = {
                    name: bound[name]
                    for name in [
                        "pairing_id",
                        "session_id",
                        "account_binding",
                        "challenge",
                        "generation",
                    ]
                }
                self.crypto = SignalSession(
                    self.controller.owner.state.identity,
                    bound["reader_keys"],
                    scope,
                    "host",
                    self.current,
                )
            # Attest exchange-key binding under the QR/stored signing pin BEFORE
            # the browser can encrypt. Routing metadata alone is insufficient.
            await self.transport.attest()
            if self.pending is None:
                # Read-only recognition of an EXISTING durable approval under a
                # fresh actual live session; never mint/rearm local approval.
                await self.transport.signal(
                    self.crypto.seal(
                        "pair.accept",
                        dict(shares=bound["shares"], generation=bound["generation"]),
                    )
                )
        elif kind == "lease":
            # The actual native socket already validated request/challenge/start.
            # Refresh the actual peer gate, never from the packet's numeric DTO.
            if self.peer is not None and self.peer._physical_current():
                self.peer._gate.refresh()
        elif kind == "signal":
            bound = self.transport.binding
            require(bound is not None)
            if self.pending is not None:
                require(
                    self.pending._review is None and self.pending.phase == "waiting",
                    "busy",
                )
                review = self.controller.owner.receive(
                    self.pending.draft,
                    bound["account_binding"],
                    bound["reader_keys"],
                    value["ciphertext"],
                )
                self.pending._review = review
                self.pending._set("review")
            else:
                require(self.crypto is not None)
                claims = self.crypto.open(value["ciphertext"])
                require(claims["kind"] in {"offer", "answer", "candidate", "restart"})
                require(not self.signals.full(), "resource_limit")
                self.signals.put_nowait(claims)  # Verified DATA; no accepted RTC peer.

    async def run(self):
        heartbeat, renewal = time.monotonic(), 0.0
        try:
            while self.current():
                self.controller.owner.expire()
                if self.receiving is None:
                    self.receiving = asyncio.create_task(self.transport.receive())
                done, _ = await asyncio.wait({self.receiving}, timeout=0.5)
                if self.receiving in done:
                    value = self.receiving.result()
                    self.receiving = None
                    await self._event(value)
                require(self.current(), "revoked")
                now = time.monotonic()
                if now - heartbeat >= 15:
                    heartbeat = now
                    await self.transport.heartbeat()
                deadline = self.transport.renewal_deadline
                require(deadline is None or now < deadline, "revoked")
                if (
                    self.pending is None
                    and self.transport.binding is not None
                    and deadline is None
                    and now - renewal >= 3
                ):
                    renewal = now
                    await self.transport.renew()
        except asyncio.CancelledError:
            self.failure = "cancelled"
            raise
        except SourceError as error:
            self.failure = error.code
        except Exception:
            self.failure = "connection_unavailable"
        finally:
            self.stop()
            await self.finish()
            if self.pending is not None and self.pending.phase not in {
                "approved",
                "ack_unknown",
                "approval_pending",
            }:
                self.pending._set("unavailable")
            self.controller._changed.set()

    async def finish(self):
        self.stop()
        if self.peer is not None:
            await self.peer.close()
        if self.receiving is not None and not self.receiving.done():
            self.receiving.cancel()
            done, _ = await asyncio.wait({self.receiving}, timeout=3)
            require(self.receiving in done, "connection_unavailable")
        if self.receiving is not None and not self.receiving.cancelled():
            self.receiving.exception()  # Observe a real terminal receiver failure.
        await self.transport.close()
        self.joined = True


class HostController:
    def __init__(self, owner, reader_origin, preview_token=None):
        require(isinstance(owner, PairingOwner))
        owner.current()
        self.owner, self.closed = owner, False
        self.service = HostService(owner, reader_origin, preview_token)
        self._preview = preview_token
        self._pending, self._runners, self._operations, self._acks = {}, {}, {}, {}
        self._operation_kinds, self._unclosed = {}, {}
        self._changed = asyncio.Event()
        self._driver = None

    def current(self):
        require(not self.closed, "cancelled")
        self.owner.current()

    def _member(self, pending):
        self.current()
        require(
            isinstance(pending, PendingPairing)
            and self._pending.get(pending.draft.pairing_id) is pending,
            "revoked",
        )

    async def _operation(self, pairing_id, kind, action):
        self.current()
        require(pairing_id not in self._unclosed, "connection_unavailable")
        previous = self._operations.get(pairing_id)
        if previous is not None and not previous.done():
            require(self._operation_kinds[pairing_id] == kind, "busy")
            # An uncertain accepted operation is observed, never duplicated.
            return await asyncio.shield(previous)
        require(
            len(self._operations) < 16 or pairing_id in self._operations,
            "resource_limit",
        )

        async def owned():
            try:
                return await action()
            except UnclosedSocket as error:
                self._unclosed[pairing_id] = error
                raise

        task = asyncio.create_task(owned())
        self._operations[pairing_id] = task
        self._operation_kinds[pairing_id] = kind
        return await asyncio.shield(task)

    def pending(self):
        self.current()
        return tuple(
            self._pending.values()
        )  # Original native objects for explicit resume.

    def service_deadline(self, pairing_id):
        self.current()
        runner = self._runners.get(pairing_id)
        require(
            runner is not None and runner.current() and runner.pending is None,
            "revoked",
        )
        return (
            runner.transport.service_deadline
        )  # Permission metadata only, no RTC/read grant.

    async def begin(self):
        self.current()
        require(len(self._pending) < 2 and len(self._operations) < 16, "resource_limit")
        draft = self.owner.start()
        pending = PendingPairing(_CREATE, draft)
        self._pending[draft.pairing_id] = pending  # BEFORE first network await.
        await self.resume(pending)
        return pending

    async def resume(self, pending):
        self._member(pending)

        async def start():
            self._member(pending)
            pair = pending.draft.pairing_id
            prior = self._runners.get(pair)
            require(prior is None or prior.joined and prior.task.done(), "busy")
            try:
                await self.service.register(pending.draft)  # SAME immutable IDs.
                self._member(pending)
                transport = await HostRendezvous.open(
                    self.owner, self.service.origin, pair, preview_token=self._preview
                )
                runner = _Runner(self, transport, pending)
                self._runners[pair] = runner
                pending._set("waiting")
                runner.task = asyncio.create_task(runner.run())
                if self.closed:
                    runner.stop()
                    raise SourceError("cancelled")
                return pending
            except BaseException:
                if pending.phase == "registering":
                    pending._set("registration_unknown")
                raise

        return await self._operation(pending.draft.pairing_id, "pending", start)

    def qr_url(self, pending):
        self._member(pending)
        require(pending.phase in {"waiting", "review"}, "connection_unavailable")
        return self.owner.qr_url(pending.draft, self.service.origin)

    async def approve(self, pending, review, indices):
        self._member(pending)
        require(
            isinstance(review, ApprovalReview)
            and pending.review is review
            and isinstance(indices, list)
            and 1 <= len(indices) <= 16,
            "revoked",
        )
        # Coalesce only the SAME actual local review/index objects. A different
        # selection must not inherit another in-flight approval's success.
        selected = tuple(indices)
        kind = ("approve", id(review), tuple(id(index) for index in selected))
        return await self._operation(
            pending.draft.pairing_id,
            kind,
            lambda: self._approve(pending, review, selected),
        )

    async def _approve(self, pending, review, indices):
        self._member(pending)
        require(
            isinstance(review, ApprovalReview)
            and pending.review is review
            and not pending._approval_lock.locked(),
            "busy",
        )
        async with pending._approval_lock:
            self.owner.describe(review)
            status = await self.service.status(pending.draft.pairing_id)
            self._member(pending)
            require(
                status["status"] == "claimed"
                and status["account_binding"] == review.account_binding
                and status["reader_keys"] == review.reader_keys
                and status["session_id"] == pending.draft.session_id
                and status["challenge"] == pending.draft.challenge,
                "revoked",
            )
            self.owner.describe(review)  # Actual live review again after await.
            pending._set("approval_pending")
            pending._ack = self.owner.approve(
                review, list(indices)
            )  # Consumes before write.
            await self._reconcile_approval(pending.draft.pairing_id)
            if not pending._ack_attempted:
                pending._ack_attempted = True  # A lost send never re-arms approval/ack.
                try:
                    await self._runners[pending.draft.pairing_id].transport.signal(
                        pending._ack
                    )
                except BaseException:
                    pending._set("ack_unknown")
                    raise
            pending._ack = None
            pending._set("approved")
            return self.approval(pending.draft.pairing_id)

    async def reconcile_approval(self, pairing_id):
        _id(pairing_id)
        return await self._operation(
            pairing_id, "reconcile", lambda: self._reconcile_approval(pairing_id)
        )

    async def _reconcile_approval(self, pairing_id):
        self.current()
        _id(pairing_id)
        row = self.owner._record(pairing_id)
        require(row is not None and row["status"] == "approved", "revoked")
        if row["generation"] == 0:
            status = await self.service.status(pairing_id)
            self.current()
            require(self.owner._record(pairing_id) == row, "revoked")
            require(
                status["status"] in {"claimed", "approved"}
                and status["account_binding"] == row["account_binding"]
                and status["reader_keys"] == row["reader_keys"]
                and status["generation"] == 0,
                "revoked",
            )
            if status["status"] == "approved":
                require(
                    status["approval_id"] == row["approval_id"]
                    and status["shares"] == row["shares"]
                )
        # gen>0 goes directly to the narrow signed scope reducer. If the durable
        # floor advanced but SQL acknowledgement failed, ordinary status MUST
        # refuse the older SQL row; it cannot be a prerequisite for reconciliation.
        value = await self.service.acknowledge(pairing_id)  # Same actual stored IDs.
        self.current()
        require(self.owner._record(pairing_id) == row, "revoked")
        self._acks[pairing_id] = value
        return self.approval(pairing_id)

    def approval(self, pairing_id):
        self.current()
        row = self.owner._record(pairing_id)
        require(
            row is not None
            and row["status"] == "approved"
            and pairing_id in self._acks,
            "revoked",
        )
        return dict(
            pairing_id=pairing_id,
            generation=row["generation"],
            shares=list(row["shares"]),
        )

    async def listen(self, pairing_id):
        self.current()
        _id(pairing_id)

        async def start():
            await self.stop(pairing_id)
            await self._reconcile_approval(pairing_id)
            self.current()
            ack = self._acks[pairing_id]
            transport = await HostRendezvous.open(
                self.owner,
                self.service.origin,
                pairing_id,
                ack["host_token"],
                self._preview,
            )
            runner = _Runner(self, transport)
            self._runners[pairing_id] = runner
            runner.task = asyncio.create_task(runner.run())
            if self.closed:
                runner.stop()
                raise SourceError("cancelled")

        return await self._operation(pairing_id, "active", start)

    async def stop(self, pairing_id):
        runner = self._runners.get(pairing_id)
        if runner is not None:
            runner.stop()
        opening = self._operations.get(pairing_id)
        if opening is not None and opening is not asyncio.current_task():
            if not opening.done():
                done, _ = await asyncio.wait({opening}, timeout=15)
                require(opening in done, "connection_unavailable")
            if not opening.cancelled():
                opening.exception()
        if pairing_id in self._unclosed:
            await self._unclosed[pairing_id].close()
            del self._unclosed[pairing_id]
        runner = self._runners.get(pairing_id)
        if runner is None:
            return
        runner.stop()
        done, _ = await asyncio.wait({runner.task}, timeout=6)
        require(runner.task in done, "connection_unavailable")
        if not runner.task.cancelled():
            runner.task.exception()
        if not runner.joined:
            await runner.finish()  # Reconcile the SAME actual retained handles.
        require(runner.joined, "connection_unavailable")

    async def revoke(self, pairing_id):
        self.current()
        runner = self._runners.get(pairing_id)
        if runner is not None:
            runner.stop()  # Stop actual peer/worker permission BEFORE store IO.
        self.owner.revoke(pairing_id)
        self._acks.pop(pairing_id, None)
        await self.stop(pairing_id)
        # An unavailable service retains the durable local tombstone for explicit
        # reconciliation. Never undo it or invent a remote success.
        await self.service.revoke(pairing_id)
        # Keep the protected tombstone; retire only observed terminal metadata.
        self._pending.pop(pairing_id, None)
        self._runners.pop(pairing_id, None)
        self._operations.pop(pairing_id, None)
        self._operation_kinds.pop(pairing_id, None)

    async def reconcile_revoked(self, pairing_id):
        self.current()
        row = self.owner._record(pairing_id)
        require(row is not None and row["status"] == "revoked", "revoked")
        await self.stop(pairing_id)
        return await self.service.revoke(pairing_id)

    async def withdraw_share(self, share_id):
        self.current()
        affected = [
            row["pairing_id"]
            for row in self.owner.state.records()
            if row["status"] == "approved" and share_id in row["shares"]
        ]
        for pair in affected:
            if pair in self._runners:
                self._runners[pair].stop()  # Before native store IO.
        self.owner.remove_share(share_id)  # Preserve remaining local grants/versions.
        for pair in affected:
            self._acks.pop(pair, None)
        for pair in affected:
            await self.stop(pair)
            if self.owner._record(pair)["status"] == "revoked":
                await self.service.revoke(pair)
            else:
                await self._reconcile_approval(
                    pair
                )  # Same durable reduced row, no new consent.
        # No automatic reconnect here. Local controls/driver choose when to listen
        # after actual old worker/socket cleanup and service acknowledgement.
        return affected

    async def retire_pending(self, pending):
        self._member(pending)
        pair = pending.draft.pairing_id
        row = self.owner._record(pair)
        require(row is not None and row["status"] != "pending", "revoked")
        await self.stop(pair)
        pending._ack = None
        self._pending.pop(pair, None)

    async def open_peer(self, pairing_id, ice_servers=None):
        self.current()
        from .rtc import HostPeer

        return await HostPeer.open(self, pairing_id, ice_servers)

    async def send_signal(self, pairing_id, kind, body):
        self.current()
        runner = self._runners.get(pairing_id)
        require(
            runner is not None
            and runner.pending is None
            and runner.current()
            and runner.crypto is not None,
            "revoked",
        )
        require(kind in {"offer", "answer", "candidate", "restart"})
        await runner.transport.signal(runner.crypto.seal(kind, body))

    async def receive_signal(self, pairing_id):
        self.current()
        runner = self._runners.get(pairing_id)
        require(
            runner is not None and runner.pending is None and runner.current(),
            "revoked",
        )
        while runner.current():
            try:
                value = await asyncio.wait_for(runner.signals.get(), 1)
                require(runner.current(), "revoked")
                return loads(dumps(value, 196608), 196608)
            except TimeoutError:
                continue
        raise SourceError("connection_unavailable")

    async def close(self):
        if self._driver is not None and not self._driver.closed:
            return await self._driver.close()
        self.closed = True
        for runner in self._runners.values():
            runner.stop()
        # Include accepted opening operations. The shielded tasks remain retained
        # if a caller cancels or this bounded observation times out.
        tasks = set(self._operations.values()) | {
            runner.task for runner in self._runners.values()
        }
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=15)
            require(not pending, "connection_unavailable")
        for runner in self._runners.values():
            runner.stop()
        more = {
            runner.task for runner in self._runners.values() if not runner.task.done()
        }
        if more:
            _, pending = await asyncio.wait(more, timeout=6)
            require(not pending, "connection_unavailable")
        for pair in list(self._operations):
            await self.stop(pair)
        require(
            all(runner.joined for runner in self._runners.values())
            and not self._unclosed,
            "connection_unavailable",
        )
        await self.service.close()
        # Owner/indices/FDs belong to the actual caller. They may retire ONLY
        # after this successful close; there is no invented file-worker join.
