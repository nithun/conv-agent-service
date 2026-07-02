"""Tools layer: Frappe tool bindings exposed to the conversation-turn loop.

A new capability = an existing whitelisted Frappe API wrapped as a tool
(P-001 in patterns.md). Never a new direct DB/state write (L-001).
Implementation lands in WBS 0.5, 1.9.
"""
