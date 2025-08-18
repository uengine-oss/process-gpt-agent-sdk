import asyncio
import logging
import sys


class ProcessGPTEventQueue(EventQueue):
    """Event queue for A2A responses from agent.

    Acts as a buffer between the agent's asynchronous execution and the
    server's response handling (e.g., streaming via SSE). Supports tapping
    to create child queues that receive the same events.
    """


    @Override
    def enqueue_event(self, event: Event):
        """supabase event table 에  insert
        Args:
            event: The event object to enqueue.
        """
        


    @Override
    def task_done(self) -> None:
        """supabase event table 에  insert
        Args:
            event: The event object to enqueue.
        """


class ProcessGPTAgentServer:

    def __init__(self, agent_executor: AgentExecutor, agent_type: str):
        self.agent_executor = agent_executor
        self.agent_type = agent_type

    def run(self):
        
        while True:
            # Simulate fetching todolist from supabase
            todolist = self.fetch_todolist_from_supabase(self.agent_type)
            if todolist:
                # Process the todolist
                queue = ProcessGPTEventQueue()
                
                self.agent_executor.execute(queue)

                break
            else:
                # Sleep for 5 seconds before polling again
                await asyncio.sleep(5)