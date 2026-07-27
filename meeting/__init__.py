"""Host-side meeting relay (Issue #42).

Bridges the ToolWindow chat dock + analysis view to remote guests through a
local in-memory relay + public tunnel. See ``meeting.relay.MeetingRelay``.
"""

from meeting.relay import MeetingRelay

__all__ = ["MeetingRelay"]
