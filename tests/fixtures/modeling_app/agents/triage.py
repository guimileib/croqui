from agents import Agent, function_tool


@function_tool
def refund(order_id: int) -> str:
    """Refund an order."""
    return "ok"


billing = Agent(name="Billing agent", instructions="handle billing", tools=[refund])
support = Agent(name="Support agent", instructions="handle support")
triage = Agent(name="Triage agent", instructions="route", handoffs=[billing, support])
