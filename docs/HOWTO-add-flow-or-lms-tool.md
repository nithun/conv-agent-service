# How to add a Glific flow or an LMS tool

Two common extensions to the conversational agent, and the exact steps for each.
They are very different in cost:

- **Adding a startable Glific flow** = *one YAML entry, no code* (a registry data
  change). Self-serve.
- **Adding an LMS/Frappe tool** = *code in two workspaces* (a Frappe endpoint on the
  tap-lms side + a client method / tool / binding here) and goes through a CR.

Read the agent's turn workflow first if you haven't: `docs/GLIFIC-FLOW-INTEGRATION.md`
and the architecture doc's §4 seam table (parent workspace `docs/architecture.md`).

---

## Part A — Add a startable Glific flow (data only)

The agent can hand a contact off to a real Glific flow mid-conversation via the
`start_glific_flow` tool. Which flows it may start is the **flow registry**
(`app/config/flow_registry.yaml`). Per **ADR-015**, adding an entry is a **MINOR
EXTENSION against the registered seam — no new ADR, no code change.**

### Steps

1. **Get two things from glific-platform / the flow owner:**
   - the flow's **current numeric `flow_id`** on the target Glific org, and
   - a **`when_to_use`** description — the sentence(s) the agent's LLM router reasons
     over to decide *when* to hand off. Write it well; it is the whole routing signal.

2. **Add an entry to `app/config/flow_registry.yaml`**, keyed by the flow **name**:

   ```yaml
   "Language Workflow":
     flow_id: "4"
     when_to_use: >
       The contact wants to change the language they are using, and a reset is
       not appropriate. Hand off to let Glific run the language selection.
   ```

   - The **name is the stable key** — the LLM's tool argument (`flow_name`) and every
     cross-reference use the name, never the integer id.
   - `flow_id` is resolved to the live id only at dispatch time.

3. **Boot the service.** The registry loader (`load_flow_registry`) validates on boot
   and **refuses to start** if any entry's `flow_id` equals the turn-relay flow id
   (`GLIFIC_TURN_FLOW_ID`) — registering the conv-AI relay flow itself as an
   orchestrated target would be a self-loop (BC-3). Fix the id if boot fails.

4. **Done.** With `FLOW_ORCHESTRATOR_ENABLED=true` and a non-empty registry, the agent
   now offers and can start the new flow. No code, no redeploy of logic.

### Guardrails / gotchas

- **Kill switches:** the whole capability is off unless `FLOW_ORCHESTRATOR_ENABLED=true`
  (default **false**), and an empty/absent registry structurally disables the tool.
- **Never key on the integer id.** Flow ids **change on re-import/republish** (the
  `26 → 80` staleness that already bit `GLIFIC_TURN_FLOW_ID`). Keep `flow_id` in sync
  whenever the flow is re-imported; a name/uuid live-lookup would remove this manual
  step (deferred — parent workspace F-091).
- **Return-to-agent** (after the started flow exits, the contact comes back to the
  agent) depends on the Glific org routing config, not this repo — it is the
  glific-platform BC-1 live-verify (parent F-091). Don't assume it; confirm it.
- **Production:** the agent starting flows is gated to internal testing — the service
  hard-refuses to boot with `FLOW_ORCHESTRATOR_ENABLED=true` under
  `ENVIRONMENT=production` (BC-4). Moving this to production needs an architecture
  decision, not a config flip.

---

## Part B — Add an LMS/Frappe tool (code — two workspaces + a CR)

Give the agent a new capability that reads/writes TAP LMS (e.g. "fetch the student's
badges"). This is a **behavior change → write a CR first** (parent workspace
`docs/change-requests/`), and it spans two repos.

### B0. The Frappe endpoint (tap-lms-frappe workspace)

The actual LMS method lives in the **tap-lms-frappe** app, not here. Send that
workspace an **outbound handover** (`docs/handovers/HANDOVER-tap-lms-frappe-…`)
specifying the method (dotted path, token auth, request/response shape), exactly like
`get_test_student_by_phone` / `get_test_student_state` were done. It stays token-auth
with the existing token user — no new credential. Get back the confirmed contract
before wiring the client.

### B1. Add the client method — `app/tools/lms_client.py`

Add an `async def` on `TapLmsClient` that calls the Frappe method and maps the
response. Mirror the existing ones (`get_next_content`, `save_submission`, …):

```python
async def get_student_badges(self, student_id: str) -> list[dict]:
    resp = await self._call("tap_lms.summer_program.badges.get_student_badges",
                            {"student_id": student_id})
    return resp.get("badges", [])
```

### B2. Add the tool schema — `app/tools/definitions.py`

A plain dict (OpenAI-tool JSON schema). Mirror `GET_NEXT_CONTENT_TOOL`:

```python
GET_STUDENT_BADGES_TOOL = {
    "name": "get_student_badges",
    "description": "Fetch the student's earned badges from the LMS.",
    "parameters": {
        "type": "object",
        "properties": {"student_id": {"type": "string"}},
        "required": ["student_id"],
    },
}
```

### B3. Offer + handle the tool — `app/core/turn_logic.py`

- **Offer it** in the right state(s): add it to the tool list built by
  `_tools_for_state` (only where it makes sense — don't offer every tool in every
  state).
- **Handle the call** in `_execute_tool_call` with a new `if call.name == "…":` branch:
  invoke the client, build a `summary`, **stash it on the session** if later turns need
  it (`session.content_reference` / a new `SessionData` field), and return
  `(name, summary)` so the reply reflects the tool's *real* return value (L-005).

```python
if call.name == "get_student_badges":
    badges = await lms_client.get_student_badges(
        _session_authoritative_id(session.identity.student_id, call, "student_id"))
    return call.name, {"badges": badges}
```

### Critical conventions (enforced — don't fight them)

- **`turn_logic.py` must NOT import anything from `app.delivery` / Glific.**
  `tests/core/test_isolation.py` fails the build otherwise. LMS-client use is fine
  (it's Frappe, not Glific); the decide-vs-dispatch split only matters for Glific
  actions (see `start_glific_flow`, where dispatch lives in `turn_processor.py`).
- **Identifier authority (L-005 / F-028):** the **session's** resolved `student_id`
  wins over whatever the model puts in the tool arguments — always resolve via
  `_session_authoritative_id(...)`, never trust a model-supplied id blindly.
- **Use only LMS-provided data.** The system prompt forbids inventing
  lessons/progress/scores — new context must come from a tool result or the prompt's
  authoritative fields, not free text.
- **Same-commit tests** for the client method, the tool binding/offer, and the
  `_execute_tool_call` branch. Run the gate: `pytest` + `ruff` + `mypy`, all green.

### Where it goes in the process

CR (change-requirement) → architecture-reviewer (a new tool is usually MINOR
EXTENSION, but a *new external contract* earns an AD-002 external-contracts row) →
feature-builder → code review + test-runner → 360° gate → `implemented`.

---

## Adding *context* to the agent (related)

If you want the agent to simply *know* more (not call a tool), extend the **system
prompt** (`app/core/prompts.py::build_system_prompt`) — add a field + template slot and
wire it where it's called in `turn_logic.py`. That's a behavior change too (a small
CR). Prefer a **tool** when the context is dynamic/large; prefer the **prompt** when
it's small and standing. Either way it must be authoritative (LMS-sourced), never
free-text the model could hallucinate from.

---

## TL;DR

| Task | Cost | Where | Process |
|---|---|---|---|
| New startable flow | 1 YAML entry | `app/config/flow_registry.yaml` | data change, MINOR EXTENSION (ADR-015 seam) |
| New LMS tool | Frappe endpoint + client + tool + binding + tests | tap-lms-frappe + `lms_client.py`/`definitions.py`/`turn_logic.py` | handover + CR → review → 360 |
| More standing context | prompt field | `app/core/prompts.py` | small CR |
