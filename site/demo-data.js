// Real artifacts from one Swarm Builder session: the generated graph document,
// the code the compile produced, and the trace of one run. Generated, not typed.
window.SWARM_DEMO = {
 "graph": {
  "id": "56b1f2f1-d659-4dd5-99bf-0cb92ac02fe3",
  "name": "Real triage",
  "entryNodeId": "classifyticket",
  "exitNodeId": "finalreply",
  "stateFields": [
   {
    "name": "ticket_text",
    "type": "str",
    "default": "\"\"",
    "description": "The original support ticket text."
   },
   {
    "name": "drafts",
    "type": "list[str]",
    "default": "None",
    "description": "List containing the specialist draft produced by the chosen branch."
   }
  ],
  "edges": [
   {
    "kind": "seq",
    "id": "e1",
    "source": "classifyticket",
    "target": "routecategory",
    "label": null
   },
   {
    "kind": "branch",
    "id": "e2",
    "source": "routecategory",
    "target": "billingagent",
    "match": "billing"
   },
   {
    "kind": "branch",
    "id": "e3",
    "source": "routecategory",
    "target": "technicalagent",
    "match": "technical"
   },
   {
    "kind": "join",
    "id": "e4",
    "source": "billingagent",
    "target": "mergedrafts"
   },
   {
    "kind": "join",
    "id": "e5",
    "source": "technicalagent",
    "target": "mergedrafts"
   },
   {
    "kind": "seq",
    "id": "e6",
    "source": "mergedrafts",
    "target": "finalreply",
    "label": null
   }
  ],
  "nodes": [
   {
    "id": "classifyticket",
    "kind": "programmatic",
    "title": "ClassifyTicket",
    "intent": "Decide whether the ticket is billing or technical with keyword rules and save the raw ticket text to state.",
    "template": null,
    "io": {
     "inputType": "str",
     "outputType": "str"
    },
    "reads": [],
    "writes": [
     "ticket_text"
    ],
    "position": {
     "x": 60.0,
     "y": 60.0
    },
    "agent": null,
    "decision": null,
    "join": null,
    "programmatic": {
     "needs": [],
     "signatureHint": "Take the raw ticket text as input; write it to state['ticket_text']; return exactly the string 'billing' or 'technical' using keyword matching (e.g. refund/charge/invoice -> billing, error/bug/crash -> technical)."
    },
    "code": {
     "pydanticStep": {
      "path": "src/swarm_workflow/steps/classifyticket.py",
      "text": "async def classifyticket(ctx: StepContext[State, Deps, str]) -> str:\n    # --- swarm:begin classifyticket ---\n    ticket_text = ctx.inputs or \"\"\n    ctx.state.ticket_text = ticket_text\n\n    text = ticket_text.lower()\n\n    billing_keywords = (\n        \"refund\",\n        \"charge\",\n        \"charged\",\n        \"invoice\",\n        \"billing\",\n        \"bill\",\n        \"payment\",\n        \"paid\",\n        \"pay\",\n        \"receipt\",\n        \"subscription\",\n        \"price\",\n        \"pricing\",\n        \"credit card\",\n        \"debit card\",\n        \"fee\",\n        \"fees\",\n        \"money\",\n        \"overcharge\",\n    )\n    technical_keywords = (\n        \"error\",\n        \"bug\",\n        \"crash\",\n        \"crashes\",\n        \"crashing\",\n        \"exception\",\n        \"fail\",\n        \"fails\",\n        \"failing\",\n        \"failure\",\n        \"broken\",\n        \"not working\",\n        \"doesn't work\",\n        \"does not work\",\n        \"slow\",\n        \"timeout\",\n        \"timed out\",\n        \"500\",\n        \"stack trace\",\n        \"login\",\n        \"log in\",\n        \"install\",\n        \"installation\",\n        \"update\",\n        \"upgrade\",\n        \"server\",\n        \"database\",\n        \"api\",\n        \"glitch\",\n    )\n\n    billing_score = sum(1 for kw in billing_keywords if kw in text)\n    technical_score = sum(1 for kw in technical_keywords if kw in text)\n\n    if billing_score > technical_score:\n        return \"billing\"\n    if technical_score > billing_score:\n        return \"technical\"\n\n    # Tie (including no keywords at all): fall back to the first keyword found\n    # in the raw ticket, then default to \"technical\".\n    positions = [\n        (text.find(kw), \"billing\")\n        for kw in billing_keywords\n        if kw in text\n    ] + [\n        (text.find(kw), \"technical\")\n        for kw in technical_keywords\n        if kw in text\n    ]\n    if positions:\n        positions.sort()\n        return positions[0][1]\n\n    return \"technical\"\n    # --- swarm:end classifyticket ---"
     },
     "langgraphNode": {
      "path": "src/swarm_workflow_lg/nodes/classifyticket.py",
      "text": "async def classifyticket_body(\n    inputs: str, state: State, model: BaseChatModel, writes: dict[str, Any]\n) -> str:\n    \"\"\"Compute this node's output. ``inputs`` is the previous node's output.\n\n    Read state with ``state[\"<field>\"]``; write it with\n    ``writes[\"<field>\"] = value``.\n    Call the model with ``await model.ainvoke([...])`` and read ``reply.text``.\n    \"\"\"\n    # --- swarm:begin classifyticket ---\n    ticket_text = inputs or \"\"\n    writes[\"ticket_text\"] = ticket_text\n\n    text = ticket_text.lower()\n\n    billing_keywords = (\n        \"refund\",\n        \"charge\",\n        \"charged\",\n        \"invoice\",\n        \"billing\",\n        \"bill\",\n        \"payment\",\n        \"paid\",\n        \"pay\",\n        \"receipt\",\n        \"subscription\",\n        \"price\",\n        \"pricing\",\n        \"credit card\",\n        \"debit card\",\n        \"fee\",\n        \"fees\",\n        \"money\",\n        \"overcharge\",\n    )\n    technical_keywords = (\n        \"error\",\n        \"bug\",\n        \"crash\",\n        \"crashes\",\n        \"crashing\",\n        \"exception\",\n        \"fail\",\n        \"fails\",\n        \"failing\",\n        \"failure\",\n        \"broken\",\n        \"not working\",\n        \"doesn't work\",\n        \"does not work\",\n        \"slow\",\n        \"timeout\",\n        \"timed out\",\n        \"500\",\n        \"stack trace\",\n        \"login\",\n        \"log in\",\n        \"install\",\n        \"installation\",\n        \"update\",\n        \"upgrade\",\n        \"server\",\n        \"database\",\n        \"api\",\n        \"glitch\",\n    )\n\n    billing_score = sum(1 for kw in billing_keywords if kw in text)\n    technical_score = sum(1 for kw in technical_keywords if kw in text)\n\n    if billing_score > technical_score:\n        return \"billing\"\n    if technical_score > billing_score:\n        return \"technical\"\n\n    # Tie (including no keywords at all): fall back to the first keyword found\n    # in the raw ticket, then default to \"technical\".\n    positions = [\n        (text.find(kw), \"billing\")\n        for kw in billing_keywords\n        if kw in text\n    ] + [\n        (text.find(kw), \"technical\")\n        for kw in technical_keywords\n        if kw in text\n    ]\n    if positions:\n        positions.sort()\n        return positions[0][1]\n\n    return \"technical\"\n    # --- swarm:end classifyticket ---\n\n\nasync def classifyticket(state: State, runtime: Runtime[Context]) -> dict[str, Any]:\n    \"\"\"Generated LangGraph node wrapper; edit the body function above instead.\"\"\"\n    writes: dict[str, Any] = {}\n    output = await classifyticket_body(\n        state.get(\"payload\"), state, runtime.context.model, writes\n    )\n    return {**writes, \"payload\": output}"
     }
    },
    "trace": {
     "status": "succeeded",
     "durationMs": 0,
     "output": "billing"
    }
   },
   {
    "id": "routecategory",
    "kind": "decision",
    "title": "RouteCategory",
    "intent": "Route the ticket to the correct specialist based on the classifier's category.",
    "template": null,
    "io": {
     "inputType": "str",
     "outputType": "str"
    },
    "reads": [],
    "writes": [],
    "position": {
     "x": 360.0,
     "y": 60.0
    },
    "agent": null,
    "decision": {
     "branches": [
      {
       "match": "billing",
       "targetNodeId": "billingagent"
      },
      {
       "match": "technical",
       "targetNodeId": "technicalagent"
      }
     ],
     "note": null
    },
    "join": null,
    "programmatic": null,
    "code": {
     "langgraphNode": {
      "path": "src/swarm_workflow_lg/nodes/routecategory.py",
      "text": "async def routecategory_body(\n    inputs: str, state: State, model: BaseChatModel, writes: dict[str, Any]\n) -> str:\n    \"\"\"Compute this node's output. ``inputs`` is the previous node's output.\n\n    Read state with ``state[\"<field>\"]``; write it with\n    ``writes[\"<field>\"] = value``.\n    Call the model with ``await model.ainvoke([...])`` and read ``reply.text``.\n    \"\"\"\n    # --- swarm:begin routecategory ---\n    return inputs  # type: ignore[return-value]\n    # --- swarm:end routecategory ---\n\n\nasync def routecategory(state: State, runtime: Runtime[Context]) -> dict[str, Any]:\n    \"\"\"Generated LangGraph node wrapper; edit the body function above instead.\"\"\"\n    writes: dict[str, Any] = {}\n    output = await routecategory_body(\n        state.get(\"payload\"), state, runtime.context.model, writes\n    )\n    return {**writes, \"payload\": output}\n\n\nROUTES: dict[str, str] = {'billing': 'billingagent', 'technical': 'technicalagent'}\n\n\ndef route_routecategory(state: State) -> str:\n    \"\"\"Pick the branch from the match value the previous node returned.\n\n    Returns the match value itself: ``add_conditional_edges`` is given\n    ``ROUTES`` as its path map, so LangGraph maps that key to the target\n    node. Returning the node name here would be a KeyError in LangGraph's\n    branch (found by a real-model compile where match != node id).\n    \"\"\"\n    value = state.get(\"payload\")\n    if isinstance(value, str) and value in ROUTES:\n        return value\n    raise ValueError(\n        f\"decision routecategory: no branch matched {value!r} \"\n        \"(expected one of 'billing', 'technical')\"\n    )"
     },
     "pydanticWiring": {
      "path": "src/swarm_workflow/graph.py",
      "text": "routecategory = builder.decision(node_id=\"routecategory\")\nroutecategory = routecategory.branch(builder.match(Literal['billing']).to(billingagent_node))\nroutecategory = routecategory.branch(builder.match(Literal['technical']).to(technicalagent_node))\nbuilder.add_edge(classifyticket_node, routecategory)"
     }
    },
    "trace": null
   },
   {
    "id": "billingagent",
    "kind": "agent",
    "title": "BillingAgent",
    "intent": "Explain the refund policy that applies to this customer's ticket.",
    "template": "chat",
    "io": {
     "inputType": "str",
     "outputType": "str"
    },
    "reads": [
     "ticket_text"
    ],
    "writes": [],
    "position": {
     "x": 660.0,
     "y": 60.0
    },
    "agent": {
     "instructions": "You are a billing support specialist. Using the customer ticket stored in state, explain the refund policy clearly and empathetically. Do not invent policy details.",
     "delegatesTo": []
    },
    "decision": null,
    "join": null,
    "programmatic": null,
    "code": {
     "pydanticStep": {
      "path": "src/swarm_workflow/steps/billingagent.py",
      "text": "async def billingagent(ctx: StepContext[State, Deps, str]) -> str:\n    # --- swarm:begin billingagent ---\n    agent = build_agent(ctx.deps.model)\n    prompt = f\"{ctx.inputs}\\n\\nContext from state:\\n\" + \"\\n\".join([f\"- ticket_text: {ctx.state.ticket_text!r}\"])\n    result = await agent.run(prompt)\n    return result.output\n    # --- swarm:end billingagent ---"
     },
     "pydanticAgent": {
      "path": "src/swarm_workflow/agents/billingagent.py",
      "text": "def build_agent(model: Model | str) -> Agent[None, str]:\n    \"\"\"Build (never at import time) the ``billingagent`` agent for this model.\n\n    The agent's ``output_type`` is the node's declared output port type,\n    so the value the step returns matches the annotation ``graph.py``\n    wires the step with. Hardcoding ``str`` here silently produced a\n    ``str`` for a node declaring a ``json`` port, which the Phase-5 dry\n    run then rejected.\n    \"\"\"\n    # --- swarm:begin billingagent ---\n    return Agent(\n        model,\n        output_type=str,\n        instructions=\"Explain the refund policy that applies to this customer's ticket.\",\n        defer_model_check=True,\n    )\n    # --- swarm:end billingagent ---"
     },
     "langgraphNode": {
      "path": "src/swarm_workflow_lg/nodes/billingagent.py",
      "text": "async def billingagent_body(\n    inputs: str, state: State, model: BaseChatModel, writes: dict[str, Any]\n) -> str:\n    \"\"\"Compute this node's output. ``inputs`` is the previous node's output.\n\n    Read state with ``state[\"<field>\"]``; write it with\n    ``writes[\"<field>\"] = value``.\n    Call the model with ``await model.ainvoke([...])`` and read ``reply.text``.\n    \"\"\"\n    # --- swarm:begin billingagent ---\n    system = SystemMessage('You are a billing support specialist. Using the customer ticket stored in state, explain the refund policy clearly and empathetically. Do not invent policy details.')\n    user_text = f\"{inputs}\\n\\nContext from state:\\n\" + \"\\n\".join([f\"- ticket_text: {state.get('ticket_text')!r}\"])\n    reply = await model.ainvoke([system, HumanMessage(user_text)])\n    output = reply.text\n    return output\n    # --- swarm:end billingagent ---\n\n\nasync def billingagent(state: State, runtime: Runtime[Context]) -> dict[str, Any]:\n    \"\"\"Generated LangGraph node wrapper; edit the body function above instead.\"\"\"\n    writes: dict[str, Any] = {}\n    output = await billingagent_body(\n        state.get(\"payload\"), state, runtime.context.model, writes\n    )\n    return {**writes, \"mergedrafts_inbox\": [output]}"
     }
    },
    "trace": {
     "status": "succeeded",
     "durationMs": 7939,
     "output": "For this ticket, the applicable policy is the **duplicate/erroneous charge refund policy**—not a standard cancellation or return refund.\n\n**What it means here:**\n- The customer says their March invoice shows **two charges for one order**.\n- If support verifies that the second charge is a duplicate, the customer is entitled to a refund for **the duplicate charge only**.\n- The original, legitimate charge remains in place.\n- No return or restocking fee applies because this is a billing error, not a product return.\n\n**Typical processing:**\n- Refund is issued to the **original payment method**.\n- If the duplicate is still pending, it may drop off automatically; if it has settled, a refund is needed.\n- Refund timing depends on the bank/payment provider, often around **5–10 business days** after approval.\n\n**What support should do:**\n1. Verify the invoice/order and confirm the two March charges are for the same order.\n2. If confirmed duplicate, refund the duplicate amount.\n3. If not confirmed, explain the finding or request more details.\n\nSo the refund policy that applies is: **verified duplicate charges are refunded, while the original charge is kept.**"
    }
   },
   {
    "id": "technicalagent",
    "kind": "agent",
    "title": "TechnicalAgent",
    "intent": "Suggest a concrete fix for the customer's technical issue.",
    "template": "chat",
    "io": {
     "inputType": "str",
     "outputType": "str"
    },
    "reads": [
     "ticket_text"
    ],
    "writes": [],
    "position": {
     "x": 660.0,
     "y": 230.0
    },
    "agent": {
     "instructions": "You are a technical support engineer. Using the customer ticket stored in state, suggest one specific, actionable fix in a friendly tone.",
     "delegatesTo": []
    },
    "decision": null,
    "join": null,
    "programmatic": null,
    "code": {
     "pydanticStep": {
      "path": "src/swarm_workflow/steps/technicalagent.py",
      "text": "async def technicalagent(ctx: StepContext[State, Deps, str]) -> str:\n    # --- swarm:begin technicalagent ---\n    agent = build_agent(ctx.deps.model)\n    prompt = f\"{ctx.inputs}\\n\\nContext from state:\\n\" + \"\\n\".join([f\"- ticket_text: {ctx.state.ticket_text!r}\"])\n    result = await agent.run(prompt)\n    return result.output\n    # --- swarm:end technicalagent ---"
     },
     "pydanticAgent": {
      "path": "src/swarm_workflow/agents/technicalagent.py",
      "text": "def build_agent(model: Model | str) -> Agent[None, str]:\n    \"\"\"Build (never at import time) the ``technicalagent`` agent for this model.\n\n    The agent's ``output_type`` is the node's declared output port type,\n    so the value the step returns matches the annotation ``graph.py``\n    wires the step with. Hardcoding ``str`` here silently produced a\n    ``str`` for a node declaring a ``json`` port, which the Phase-5 dry\n    run then rejected.\n    \"\"\"\n    # --- swarm:begin technicalagent ---\n    return Agent(\n        model,\n        output_type=str,\n        instructions=\"Suggest a concrete fix for the customer's technical issue.\",\n        defer_model_check=True,\n    )\n    # --- swarm:end technicalagent ---"
     },
     "langgraphNode": {
      "path": "src/swarm_workflow_lg/nodes/technicalagent.py",
      "text": "async def technicalagent_body(\n    inputs: str, state: State, model: BaseChatModel, writes: dict[str, Any]\n) -> str:\n    \"\"\"Compute this node's output. ``inputs`` is the previous node's output.\n\n    Read state with ``state[\"<field>\"]``; write it with\n    ``writes[\"<field>\"] = value``.\n    Call the model with ``await model.ainvoke([...])`` and read ``reply.text``.\n    \"\"\"\n    # --- swarm:begin technicalagent ---\n    system = SystemMessage('You are a technical support engineer. Using the customer ticket stored in state, suggest one specific, actionable fix in a friendly tone.')\n    user_text = f\"{inputs}\\n\\nContext from state:\\n\" + \"\\n\".join([f\"- ticket_text: {state.get('ticket_text')!r}\"])\n    reply = await model.ainvoke([system, HumanMessage(user_text)])\n    output = reply.text\n    return output\n    # --- swarm:end technicalagent ---\n\n\nasync def technicalagent(state: State, runtime: Runtime[Context]) -> dict[str, Any]:\n    \"\"\"Generated LangGraph node wrapper; edit the body function above instead.\"\"\"\n    writes: dict[str, Any] = {}\n    output = await technicalagent_body(\n        state.get(\"payload\"), state, runtime.context.model, writes\n    )\n    return {**writes, \"mergedrafts_inbox\": [output]}"
     }
    },
    "trace": null
   },
   {
    "id": "mergedrafts",
    "kind": "join",
    "title": "MergeDrafts",
    "intent": "Collect the single specialist draft produced by whichever branch executed.",
    "template": null,
    "io": {
     "inputType": "str",
     "outputType": "list[str]"
    },
    "reads": [],
    "writes": [
     "drafts"
    ],
    "position": {
     "x": 960.0,
     "y": 60.0
    },
    "agent": null,
    "decision": null,
    "join": {
     "reducer": "list_append",
     "initialFactory": null
    },
    "programmatic": null,
    "code": {
     "langgraphNode": {
      "path": "src/swarm_workflow_lg/nodes/mergedrafts.py",
      "text": "async def mergedrafts_body(\n    inputs: str, state: State, model: BaseChatModel, writes: dict[str, Any]\n) -> list[str]:\n    \"\"\"Compute this node's output. ``inputs`` is the previous node's output.\n\n    Read state with ``state[\"<field>\"]``; write it with\n    ``writes[\"<field>\"] = value``.\n    Call the model with ``await model.ainvoke([...])`` and read ``reply.text``.\n    \"\"\"\n    # --- swarm:begin mergedrafts ---\n    # Fan-in: the arms already reduced into this join's channel.\n    output = state.get(\"mergedrafts_inbox\")\n    return output  # type: ignore[return-value]\n    # --- swarm:end mergedrafts ---\n\n\nasync def mergedrafts(state: State, runtime: Runtime[Context]) -> dict[str, Any]:\n    \"\"\"Generated LangGraph node wrapper; edit the body function above instead.\"\"\"\n    writes: dict[str, Any] = {}\n    output = await mergedrafts_body(\n        state.get(\"payload\"), state, runtime.context.model, writes\n    )\n    return {**writes, \"payload\": output}"
     },
     "pydanticWiring": {
      "path": "src/swarm_workflow/graph.py",
      "text": "mergedrafts = builder.join(reduce_list_append, initial_factory=list, node_id=\"mergedrafts\")\nbuilder.add_edge(billingagent_node, mergedrafts)\nbuilder.add_edge(technicalagent_node, mergedrafts)\nbuilder.add_edge(mergedrafts, finalreply_node)"
     }
    },
    "trace": null
   },
   {
    "id": "finalreply",
    "kind": "agent",
    "title": "FinalReply",
    "intent": "Write the two-sentence customer reply using the ticket and the specialist draft.",
    "template": "chat",
    "io": {
     "inputType": "list[str]",
     "outputType": "str"
    },
    "reads": [
     "ticket_text",
     "drafts"
    ],
    "writes": [],
    "position": {
     "x": 1260.0,
     "y": 60.0
    },
    "agent": {
     "instructions": "Write exactly two sentences to the customer: first acknowledge their issue, then state the resolution drawn from the specialist draft. Keep it warm and professional.",
     "delegatesTo": []
    },
    "decision": null,
    "join": null,
    "programmatic": null,
    "code": {
     "pydanticStep": {
      "path": "src/swarm_workflow/steps/finalreply.py",
      "text": "async def finalreply(ctx: StepContext[State, Deps, list[str]]) -> str:\n    # --- swarm:begin finalreply ---\n    agent = build_agent(ctx.deps.model)\n    prompt = f\"{ctx.inputs}\\n\\nContext from state:\\n\" + \"\\n\".join([f\"- ticket_text: {ctx.state.ticket_text!r}\", f\"- drafts: {ctx.state.drafts!r}\"])\n    result = await agent.run(prompt)\n    return result.output\n    # --- swarm:end finalreply ---"
     },
     "pydanticAgent": {
      "path": "src/swarm_workflow/agents/finalreply.py",
      "text": "def build_agent(model: Model | str) -> Agent[None, str]:\n    \"\"\"Build (never at import time) the ``finalreply`` agent for this model.\n\n    The agent's ``output_type`` is the node's declared output port type,\n    so the value the step returns matches the annotation ``graph.py``\n    wires the step with. Hardcoding ``str`` here silently produced a\n    ``str`` for a node declaring a ``json`` port, which the Phase-5 dry\n    run then rejected.\n    \"\"\"\n    # --- swarm:begin finalreply ---\n    return Agent(\n        model,\n        output_type=str,\n        instructions='Write the two-sentence customer reply using the ticket and the specialist draft.',\n        defer_model_check=True,\n    )\n    # --- swarm:end finalreply ---"
     },
     "langgraphNode": {
      "path": "src/swarm_workflow_lg/nodes/finalreply.py",
      "text": "async def finalreply_body(\n    inputs: list[str], state: State, model: BaseChatModel, writes: dict[str, Any]\n) -> str:\n    \"\"\"Compute this node's output. ``inputs`` is the previous node's output.\n\n    Read state with ``state[\"<field>\"]``; write it with\n    ``writes[\"<field>\"] = value``.\n    Call the model with ``await model.ainvoke([...])`` and read ``reply.text``.\n    \"\"\"\n    # --- swarm:begin finalreply ---\n    system = SystemMessage('Write exactly two sentences to the customer: first acknowledge their issue, then state the resolution drawn from the specialist draft. Keep it warm and professional.')\n    user_text = f\"{inputs}\\n\\nContext from state:\\n\" + \"\\n\".join([f\"- ticket_text: {state.get('ticket_text')!r}\", f\"- drafts: {state.get('drafts')!r}\"])\n    reply = await model.ainvoke([system, HumanMessage(user_text)])\n    output = reply.text\n    return output\n    # --- swarm:end finalreply ---\n\n\nasync def finalreply(state: State, runtime: Runtime[Context]) -> dict[str, Any]:\n    \"\"\"Generated LangGraph node wrapper; edit the body function above instead.\"\"\"\n    writes: dict[str, Any] = {}\n    output = await finalreply_body(\n        state.get(\"payload\"), state, runtime.context.model, writes\n    )\n    return {**writes, \"payload\": output}"
     }
    },
    "trace": {
     "status": "succeeded",
     "durationMs": 3058,
     "output": "Thanks for bringing this to our attention—we’ll verify your March invoice, and if the second charge is confirmed as a duplicate for the same order, we’ll refund that duplicate amount to your original payment method while the original charge remains in place. Refunds typically take 5–10 business days after approval, and if the duplicate is still pending it may drop off automatically; we’ll follow up once we’ve reviewed the charges."
    }
   }
  ]
 },
 "run": {
  "input": "My invoice shows two charges for March, but I only ordered once. Please refund the duplicate.",
  "output": "Thanks for bringing this to our attention—we’ll verify your March invoice, and if the second charge is confirmed as a duplicate for the same order, we’ll refund that duplicate amount to your original payment method while the original charge remains in place. Refunds typically take 5–10 business days after approval, and if the duplicate is still pending it may drop off automatically; we’ll follow up once we’ve reviewed the charges.",
  "durationMs": 11003,
  "model": "deepseek:deepseek-flash",
  "sequence": [
   "classifyticket",
   "routecategory",
   "billingagent",
   "mergedrafts",
   "finalreply"
  ],
  "state": {
   "ticket_text": "My invoice shows two charges for March, but I only ordered once. Please refund the duplicate.",
   "drafts": null
  }
 },
 "description": "Take a support ticket as plain text. Classify it as \"billing\" or \"technical\" with a rule-based step that returns exactly one of those words and stores the original ticket text in state. Route on the classification: billing tickets go to an agent that explains refund policy; technical tickets go to an agent that suggests a fix. Both agents read the original ticket from state. Finish with an agent that writes a two-sentence customer reply."
};
