"""theygent control-plane — the cockpit API (M3).

The thinnest possible control-plane that owns a single request end to end: a prompt
comes in over the theygent-native ``/runs`` surface, the control-plane resolves which
logical model to call, calls the inference plane **over the OpenAI-compatible HTTP
seam** (never in-process — M3 §3.1, the one hard-to-reverse rule), streams the answer
back, and closes out a ``Run``. The ``Run`` is the identity that memory, observability,
audit and graph execution will later hang off without reshaping the request path.
"""

from __future__ import annotations

from theygent_control_plane.app import create_app

__all__ = ["create_app"]
