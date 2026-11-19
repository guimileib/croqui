from typing import Literal

from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode


@tool
def search_orders(customer_id: int) -> str:
    """Look up the orders of a customer."""
    return ""


def call_model(state):
    """Ask the model what to do next."""
    return state


def route(state) -> Literal["tools", "__end__"]:
    if state["messages"][-1].tool_calls:
        return "tools"
    return END


builder = StateGraph(dict)
builder.add_node("agent", call_model)
builder.add_node("tools", ToolNode([search_orders]))
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", route)
builder.add_edge("tools", "agent")
support_graph = builder.compile()
