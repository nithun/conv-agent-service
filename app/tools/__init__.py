"""Tools layer: Frappe tool bindings exposed to the conversation-turn loop.

A new capability = an existing whitelisted Frappe API wrapped as a tool
(P-001 in patterns.md). Never a new direct DB/state write (L-001).
Ported from `tap-buddy/src/tap_buddy/clients/lms_client.py` +
`core/tools.py` (CR-004 Task 12): `lms_client.py` is the real Frappe HTTP
client (exactly 4 whitelisted methods), `lms_mock.py` a fixture-backed
stand-in for tests/offline dev, `definitions.py` the tool schemas bound to
those 4 methods for the conversation-turn loop.
"""
