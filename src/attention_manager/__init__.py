"""attention-manager — packet queue and escalation bus for Amplifier worker sessions.

Step 1 scope: packet model, disk queue, CLI. The on-disk packet file format is
the contract; it is documented authoritatively in ``context/packet-schema.md``.
"""

from .packet import Packet
from .packet import Resolution
from .queue import PacketQueue

__all__ = ["Packet", "PacketQueue", "Resolution"]
