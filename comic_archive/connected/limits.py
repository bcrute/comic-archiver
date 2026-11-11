"""How long each layer of the host waits for the one beneath it to close.

Every wait covers all that it waits on, so a slow but ordinary close further down is seen to finish
instead of being given up on from above and left as cleanup still owed.
"""

WORKER_CLOSE = 8  # The thread that reads files for one reader.
NATIVE_CLOSE = 8  # The peer connection itself.
PEER_TASK = 12  # A peer's own task, once it has been told to stop.
PEER_CLEANUP = WORKER_CLOSE + NATIVE_CLOSE + 2
PEER_CLOSE = PEER_TASK + PEER_CLEANUP
RECEIVE_CLOSE = 3  # A pairing's wait for the next message, once cancelled.
RUNNER_CLOSE = PEER_CLOSE + RECEIVE_CLOSE + 3  # A pairing's connection: its peer, its receiver, its socket.
OPENING = 15  # A call to the service that is still out.
PAIRING_STOP = OPENING + RUNNER_CLOSE
SLOT_STOP = PAIRING_STOP + 4  # The task that keeps one pairing available, which ends by stopping it.
