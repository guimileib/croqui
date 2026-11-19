from crewai import Agent, Crew, Process, Task

researcher = Agent(role="Analista de mercado", goal="pesquisar", tools=[], allow_delegation=False)
writer = Agent(role="Redator", goal="escrever")

research = Task(description="Pesquisar concorrentes", agent=researcher)
report = Task(description="Escrever o relatório", agent=writer, context=[research])

crew = Crew(agents=[researcher, writer], tasks=[research, report], process=Process.sequential)
