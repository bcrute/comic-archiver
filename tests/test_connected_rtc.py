"""SDP parsing DATA and peer-setup retry policy; never a successful native peer."""

import asyncio
import importlib.util
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

AVAILABLE = (
    importlib.util.find_spec("aiortc") is not None
    and importlib.util.find_spec("jwcrypto") is not None
)
if AVAILABLE:
    from comic_archive.connected.controller import HostController
    from comic_archive.connected.driver import HostDriver, _Slot
    from comic_archive.connected.files import Index, SharedRoot, SourceError
    from comic_archive.connected.pairing import PairingOwner
    from comic_archive.connected.rtc import data_sdp
    from comic_archive.connected.state import HostState


@unittest.skipUnless(
    AVAILABLE, "Actual connected dependencies required; skip is no qualification"
)
class ConnectedRtcSdpTests(unittest.TestCase):
    def test_data_sdp_requires_one_application_and_a_bounded_sha256_binding(self):
        # Deliberate NON-CRYPTO DATA. No RTCPeerConnection/HostPeer/Account is made.
        fingerprint = ":".join(["AA"] * 32)
        sdp = (
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
            "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\nc=IN IP4 0.0.0.0\r\n"
            "a=mid:0\r\na=sctp-port:5000\r\na=setup:actpass\r\na=fingerprint:sha-256 "
            + fingerprint
            + "\r\n"
        )
        self.assertEqual(data_sdp(sdp), fingerprint)
        for bad in [
            sdp.replace("m=application", "m=audio"),
            sdp + sdp[sdp.index("m=") :],
            sdp.replace(fingerprint, "AA"),
            sdp.replace("sha-256", "sha-1"),
            "x" * 65537,
        ]:
            with self.assertRaises((SourceError, ValueError, AssertionError)):
                data_sdp(bad)


class _WaitingRunner:
    """Listening socket stand-in holding one offer; it never grants a peer."""

    peer, failure = None, None

    def __init__(self):
        self.signals = asyncio.Queue()
        self.signals.put_nowait({"kind": "offer"})

    def current(self):
        return True

    def stop(self):
        pass


@unittest.skipUnless(
    AVAILABLE and os.name == "posix",
    "Actual connected dependencies required; skip is no qualification",
)
class ConnectedDriverRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        base = Path(work.name)
        (base / "shared").mkdir()
        self.root = SharedRoot(base / "shared")
        self.addCleanup(self.root.close)
        self.index = Index(base / "index/private.sqlite", self.root)
        self.addCleanup(self.index.close)
        state = HostState.open(base / "host", roots=[self.root], create=True)
        self.owner = PairingOwner(state, {self.index.share_id: self.index})
        self.controller = HostController(self.owner, "http://127.0.0.1:1")
        self.driver = HostDriver.start(self.controller)

    async def asyncTearDown(self):
        await self.driver.close()
        self.owner.close()

    async def test_failed_peer_setup_backs_off_but_account_refusal_needs_attention(
        self,
    ):
        pair, runner, refusals = str(uuid.uuid4()), _WaitingRunner(), []

        async def listen(pairing_id):
            if refusals:
                raise SourceError(refusals[0])
            self.controller._runners[pairing_id] = runner

        async def open_peer(pairing_id, servers):
            runner.signals.get_nowait()
            raise SourceError("integrity_failed")  # One garbled offer.

        async def stop(pairing_id):
            self.controller._runners.pop(pairing_id, None)

        with (
            patch.object(self.controller, "listen", listen),
            patch.object(self.controller, "open_peer", open_peer),
            patch.object(self.controller, "stop", stop),
        ):
            slot = _Slot(pair)
            self.driver._slots[pair] = slot
            slot.task = asyncio.create_task(self.driver._serve(slot))
            for _ in range(200):
                if slot.phase == "retry_wait":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(slot.phase, "retry_wait")
            self.assertTrue(slot.enabled)
            self.assertEqual((slot.error, slot.failures), ("integrity_failed", 1))
            refusals.append("auth_required")  # The account side now refuses.
            await asyncio.wait_for(slot.task, 5)
        self.assertFalse(slot.enabled)
        self.assertEqual(
            (slot.phase, slot.error), ("needs_attention", "auth_required")
        )
