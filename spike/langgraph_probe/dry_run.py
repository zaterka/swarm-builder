from __future__ import annotations
import asyncio
from langchain_core.language_models import FakeListChatModel, GenericFakeChatModel
from langchain_core.messages import AIMessage
from lg_probe.graph import graph

print("nodes:", sorted(graph.get_graph().nodes))
mermaid = graph.get_graph().draw_mermaid()
print("--- mermaid ---"); print(mermaid); print("--- end ---")

class KeylessFake(FakeListChatModel):
    def bind_tools(self, tools, **kwargs):  # accept & ignore tools
        return self

async def main():
    fake = KeylessFake(responses=["fake reply"])
    out = await graph.ainvoke({"payload": "a long topic string"}, config={"configurable": {"model": fake}})
    print("final:", out["payload"], "| topic:", out["topic"])
    # streaming updates -> per-node events
    seen = []
    async for update in graph.astream({"payload": "short"}, config={"configurable": {"model": fake}}, stream_mode="updates"):
        seen.append(list(update.keys()))
    print("updates order:", seen)
    # does plain FakeListChatModel support bind_tools?
    try:
        FakeListChatModel(responses=["x"]).bind_tools([])
        print("FakeListChatModel.bind_tools: OK")
    except Exception as e:
        print("FakeListChatModel.bind_tools:", type(e).__name__, str(e)[:80])
    # tool-calling fake via GenericFakeChatModel
    tc = AIMessage(content="", tool_calls=[{"name": "helper", "args": {"query": "q"}, "id": "c1", "type": "tool_call"}])
    class ToolFake(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs): return self
    gf = ToolFake(messages=iter([tc, AIMessage(content="done after tool")]))
    from lg_probe.graph import coordinator
    print("coordinator with tool call:", await coordinator({"payload": "x"}, {"configurable": {"model": gf}}))
asyncio.run(main())
