from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Any
from typing_extensions import TypedDict
from langchain_core.language_models import BaseChatModel, FakeListChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

@dataclass
class Context:
    model: BaseChatModel

class State(TypedDict, total=False):
    payload: Any

async def step(state: State, runtime: Runtime[Context]) -> dict:
    reply = await runtime.context.model.ainvoke(str(state["payload"]))
    return {"payload": reply.text}

b = StateGraph(State, context_schema=Context)
b.add_node("step", step); b.add_edge(START, "step"); b.add_edge("step", END)
g = b.compile()
async def main():
    fake = FakeListChatModel(responses=["via runtime"])
    print(await g.ainvoke({"payload": "hi"}, context=Context(model=fake)))
    async for u in g.astream({"payload": "hi"}, context=Context(model=fake), stream_mode="updates"):
        print("update:", u)
    from langchain.chat_models import init_chat_model
    import inspect; print("init_chat_model sig:", str(inspect.signature(init_chat_model))[:200])
asyncio.run(main())
