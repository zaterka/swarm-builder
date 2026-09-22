"""Hand-written LangGraph probe: linear -> decision -> fan-out/join -> orchestrator-with-tool."""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal

from typing_extensions import TypedDict

from langchain.messages import HumanMessage, SystemMessage
from langchain.tools import tool
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph


class State(TypedDict, total=False):
    payload: Any                                   # the value flowing between steps
    topic: str                                     # canvas stateField
    merge_items: Annotated[list[str], operator.add]  # join channel (list_append)


def get_model(config: RunnableConfig) -> BaseChatModel:
    model = config.get("configurable", {}).get("model")
    if model is None:
        raise RuntimeError("no model in config['configurable']['model']")
    return model


async def intake(state: State) -> dict:
    text = str(state.get("payload", "")).strip()
    return {"payload": text, "topic": text}


async def classify(state: State) -> dict:
    return {"payload": "big" if len(state["payload"]) > 10 else "small"}


def route(state: State) -> Literal["big_step", "small_step"]:
    return "big_step" if state["payload"] == "big" else "small_step"


async def big_step(state: State, config: RunnableConfig) -> dict:
    model = get_model(config)
    reply = await model.ainvoke([SystemMessage("You handle big topics."), HumanMessage(state["topic"])])
    return {"payload": reply.text}


async def small_step(state: State, config: RunnableConfig) -> dict:
    model = get_model(config)
    reply = await model.ainvoke([SystemMessage("You handle small topics."), HumanMessage(state["topic"])])
    return {"payload": reply.text}


async def split(state: State) -> dict:
    return {"payload": state["payload"], "merge_items": []}


async def left(state: State) -> dict:
    return {"merge_items": [f"left:{state['payload']}"]}


async def right(state: State) -> dict:
    return {"merge_items": [f"right:{state['payload']}"]}


async def merge(state: State) -> dict:
    return {"payload": list(state["merge_items"])}


@tool
async def helper(query: str) -> str:
    """Delegate to the helper child agent."""
    return f"helper saw {query}"


async def coordinator(state: State, config: RunnableConfig) -> dict:
    model = get_model(config).bind_tools([helper])
    messages: list = [SystemMessage("Coordinate. Use tools when useful."), HumanMessage(str(state["payload"]))]
    for _ in range(4):
        reply = await model.ainvoke(messages)
        messages.append(reply)
        if not reply.tool_calls:
            return {"payload": reply.text}
        for call in reply.tool_calls:
            messages.append(await helper.ainvoke(call))
    return {"payload": messages[-1].text}


builder = StateGraph(State)
for name, fn in [
    ("intake", intake), ("classify", classify), ("big_step", big_step), ("small_step", small_step),
    ("split", split), ("left", left), ("right", right), ("merge", merge), ("coordinator", coordinator),
]:
    builder.add_node(name, fn)
builder.add_edge(START, "intake")
builder.add_edge("intake", "classify")
builder.add_conditional_edges("classify", route, {"big_step": "big_step", "small_step": "small_step"})
builder.add_edge("big_step", "split")
builder.add_edge("small_step", "split")
builder.add_edge("split", "left")
builder.add_edge("split", "right")
builder.add_edge(["left", "right"], "merge")
builder.add_edge("merge", "coordinator")
builder.add_edge("coordinator", END)
graph = builder.compile()
