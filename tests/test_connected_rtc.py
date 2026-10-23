"""SDP parsing DATA only, never a successful native peer/lease fixture."""
import importlib.util
import unittest

AVAILABLE = importlib.util.find_spec('aiortc') is not None and importlib.util.find_spec('jwcrypto') is not None
if AVAILABLE:
    from comic_archive.connected.files import SourceError
    from comic_archive.connected.rtc import data_sdp


@unittest.skipUnless(AVAILABLE, 'Actual connected dependencies required; skip is no qualification')
class ConnectedRtcSdpTests(unittest.TestCase):
    def test_data_sdp_requires_one_application_and_a_bounded_sha256_binding(self):
        # Deliberate NON-CRYPTO DATA. No RTCPeerConnection/HostPeer/Account is made.
        fingerprint = ':'.join(['AA'] * 32)
        sdp = ('v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n'
               'm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\nc=IN IP4 0.0.0.0\r\n'
               'a=mid:0\r\na=sctp-port:5000\r\na=setup:actpass\r\na=fingerprint:sha-256 ' + fingerprint + '\r\n')
        self.assertEqual(data_sdp(sdp), fingerprint)
        for bad in [sdp.replace('m=application','m=audio'),sdp+sdp[sdp.index('m='):],
                    sdp.replace(fingerprint,'AA'),sdp.replace('sha-256','sha-1'), 'x'*65537]:
            with self.assertRaises((SourceError, ValueError, AssertionError)):
                data_sdp(bad)
