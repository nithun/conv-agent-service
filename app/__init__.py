"""TAP LMS conversational agent service.

FastAPI microservice between Glific (WhatsApp delivery) and TAP LMS/Frappe
(system of record). See the workspace's docs/architecture.md for the
system-level design; this package mirrors its top-down shape:

    ingress -> queue -> worker -> llm/tools -> delivery

Each subpackage is currently a scaffold (WBS 0.1) — business logic lands in
later tasks (0.3-0.6, Phase 1).
"""
