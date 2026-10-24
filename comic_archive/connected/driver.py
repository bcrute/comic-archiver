"""Normal host availability over real retained controller/RTC/worker owners.

No described Account/Socket/Peer/Lease/Runtime or injected positive transport.
Local start is an availability choice, never a substitute for durable consent.
"""

import asyncio

from .controller import HostController
from .files import SourceError
from .jsoncodec import require
from .rtc import configuration
from .wire import _id

_CREATE = object()
_ATTENTION = {
    "auth_required",
    "revoked",
    "share_removed",
    "integrity_failed",
    "unsupported_format",
}


class _Slot:
    def __init__(self, pair):
        self.pair, self.enabled = pair, True
        self.task, self.joined = None, False
        self.phase, self.error, self.failures = "starting", None, 0
        self.wake = asyncio.Event()


class HostDriver:
    def __init__(self, token, controller, servers):
        require(token is _CREATE)
        self.controller, self._servers = controller, servers
        self._slots, self.closed, self.joined = {}, False, False

    @classmethod
    def start(cls, controller, ice_servers=None):
        require(isinstance(controller, HostController))
        controller.current()
        require(controller._driver is None, "busy")
        servers = [] if ice_servers is None else ice_servers
        # Copy bounded DATA into SDK config before tasks can open a native PC;
        # it grants no accepted peer, and no default public relay is inserted.
        from .jsoncodec import dumps, loads

        servers = loads(dumps(servers, 8192), 8192)
        configuration(servers)
        result = cls(_CREATE, controller, servers)
        controller._driver = result  # Retained before any native opening task.
        return result

    def current(self):
        require(not self.closed, "cancelled")
        self.controller.current()

    def enable(self, pairing_id):
        self.current()
        _id(pairing_id)
        row = self.controller.owner._record(pairing_id)
        require(
            row is not None
            and row["status"] == "approved"
            and all(share in self.controller.owner._indices for share in row["shares"]),
            "revoked",
        )
        for share in row["shares"]:
            self.controller.owner._indices[share].root.current()
        previous = self._slots.get(pairing_id)
        if previous is not None:
            if (
                previous.enabled
                and previous.task is not None
                and not previous.task.done()
            ):
                return  # SAME actual task, not another listen/peer.
            require(previous.joined and previous.task.done(), "connection_unavailable")
            if not previous.task.cancelled():
                previous.task.exception()
        require(pairing_id in self._slots or len(self._slots) < 16, "resource_limit")
        slot = _Slot(pairing_id)
        self._slots[pairing_id] = slot
        slot.task = asyncio.create_task(self._serve(slot))

    def pause(self, pairing_id):
        _id(pairing_id)
        slot = self._slots.get(pairing_id)
        if slot is not None:
            slot.enabled = False
            slot.wake.set()
        runner = self.controller._runners.get(pairing_id)
        if runner is not None:
            runner.stop()  # Actual peer/file permission retires synchronously.

    async def stop(self, pairing_id):
        self.pause(pairing_id)
        slot = self._slots.get(pairing_id)
        if slot is not None:
            done, _ = await asyncio.wait({slot.task}, timeout=25)
            require(slot.task in done, "connection_unavailable")
            if not slot.task.cancelled():
                slot.task.exception()
        await self.controller.stop(pairing_id)
        if slot is not None and not slot.joined:
            slot.joined, slot.phase = (
                True,
                "stopped",
            )  # SAME native cleanup really observed.

    async def _serve(self, slot):
        try:
            while slot.enabled and not self.closed:
                renewing = False
                try:
                    self.current()
                    slot.phase = "starting"
                    await self.controller.listen(slot.pair)
                    runner = self.controller._runners[slot.pair]
                    slot.phase = "waiting"
                    while slot.enabled and not self.closed and runner.current():
                        if self.controller.renewal_due(slot.pair):
                            renewing, slot.phase = True, "renewing"
                            break
                        if runner.peer is None and not runner.signals.empty():
                            slot.phase = "connecting"
                            await self.controller.open_peer(slot.pair, self._servers)
                            slot.phase, slot.error, slot.failures = "connected", None, 0
                        if runner.peer is not None and not runner.peer.current():
                            break
                        await asyncio.sleep(0.1)
                    slot.error = runner.failure or "connection_unavailable"
                except SourceError as error:
                    slot.error = error.code
                except asyncio.CancelledError:
                    slot.enabled = False
                    raise
                except Exception:
                    slot.error = "connection_unavailable"
                # No reconnect after an uncertain native close/join. Retain the
                # SAME controller/task/worker/FD handles and require attention.
                await self.controller.stop(slot.pair)
                if renewing and slot.enabled and not self.closed:
                    continue  # SAME availability task, fresh admission after rotation.
                if slot.error in _ATTENTION:
                    slot.enabled = False
                if not slot.enabled or self.closed:
                    break
                slot.failures = min(slot.failures + 1, 6)
                slot.phase = "retry_wait"
                try:
                    await asyncio.wait_for(
                        slot.wake.wait(), min(30, 2 ** (slot.failures - 1))
                    )
                except TimeoutError:
                    pass
        finally:
            slot.enabled = False
            slot.phase = "stopping"
            try:
                await self.controller.stop(slot.pair)
            except BaseException:
                slot.phase, slot.error = "cleanup_pending", "connection_unavailable"
                raise
            slot.joined = True
            slot.phase = "needs_attention" if slot.error in _ATTENTION else "stopped"

    async def wait_listening(self, pairing_id, maximum=20):
        _id(pairing_id)
        end = asyncio.get_running_loop().time() + maximum
        while True:
            self.current()
            slot = self._slots.get(pairing_id)
            require(
                slot is not None and slot.enabled and not slot.task.done(),
                (slot.error or "cancelled") if slot else "revoked",
            )
            runner = self.controller._runners.get(pairing_id)
            if (
                slot.phase in {"waiting", "connecting", "connected"}
                and runner is not None
                and runner.current()
            ):
                return  # Actual native waiting socket, not a phase string alone.
            require(asyncio.get_running_loop().time() < end, "connection_unavailable")
            await asyncio.sleep(0.02)

    async def wait_peer(self, pairing_id, maximum=45):
        _id(pairing_id)
        end = asyncio.get_running_loop().time() + maximum
        while True:
            self.current()
            slot = self._slots.get(pairing_id)
            require(
                slot is not None and slot.enabled and not slot.task.done(),
                (slot.error or "cancelled") if slot else "revoked",
            )
            runner = self.controller._runners.get(pairing_id)
            if (
                slot.phase == "connected"
                and runner is not None
                and runner.peer is not None
                and runner.peer.current()
                and runner.peer.worker is not None
            ):
                return runner.peer  # SAME accepted native owner; no reconstruction.
            require(asyncio.get_running_loop().time() < end, "connection_unavailable")
            await asyncio.sleep(0.02)

    async def approve(self, pending, review, indices):
        self.current()
        result = await self.controller.approve(pending, review, indices)
        await self.controller.retire_pending(pending)
        self.enable(result["pairing_id"])
        return result

    async def revoke(self, pairing_id):
        self.current()
        self.pause(pairing_id)
        # The controller persists its native tombstone BEFORE its first await.
        # Waiting for driver/socket shutdown first could revive consent on crash.
        result = await self.controller.revoke(pairing_id)
        await self.stop(pairing_id)
        return result

    async def withdraw_share(self, share_id):
        self.current()
        _id(share_id)
        affected = [
            row["pairing_id"]
            for row in self.controller.owner.state.records()
            if row["status"] == "approved" and share_id in row["shares"]
        ]
        resume = {
            pair
            for pair in affected
            if pair in self._slots and self._slots[pair].enabled
        }
        for pair in affected:
            self.pause(pair)  # ALL affected permissions before any await/store IO.
        self.controller.owner.remove_share(
            share_id
        )  # Durable local reduction BEFORE first await.
        for pair in affected:
            self.controller._acks.pop(pair, None)
        for pair in affected:
            await self.stop(pair)
            if self.controller.owner._record(pair)["status"] == "revoked":
                await self.controller.service.revoke(pair)
            else:
                await self.controller.reconcile_approval(pair)
        for pair in resume:
            if self.controller.owner._record(pair)["status"] == "approved":
                self.enable(pair)
        return affected

    def status(self):
        self.current()
        result = []
        for pair, slot in self._slots.items():
            runner = self.controller._runners.get(pair)
            accepted = (
                runner is not None and runner.peer is not None and runner.peer.current()
            )
            result.append(
                dict(
                    pairing_id=pair,
                    phase=slot.phase,
                    error=slot.error,
                    connected=bool(accepted),
                    enabled=slot.enabled,
                )
            )
        return result  # Display only, never accepted by a permission constructor.

    async def close(self):
        self.closed = True
        for pair in self._slots:
            self.pause(pair)
        tasks = {slot.task for slot in self._slots.values()}
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=25)
            require(not pending, "connection_unavailable")
        for slot in self._slots.values():
            if not slot.task.cancelled():
                slot.task.exception()
            if not slot.joined:
                # Observe/close the SAME retained native resources on a later
                # explicit cleanup attempt; never start another availability job.
                await self.controller.stop(slot.pair)
                slot.joined, slot.phase = True, "stopped"
        await self.controller.close()
        self.joined = True
