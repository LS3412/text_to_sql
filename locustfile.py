"""
Locust load / concurrency test for the A2A Handler (§9).

Run:
    pip install locust
    locust -f locustfile.py --host=http://localhost:8000
    # open http://localhost:8089 — set users (e.g. 200) and spawn rate (e.g. 20/s)

Headless:
    locust -f locustfile.py --host=http://localhost:8000 \
           --users 200 --spawn-rate 20 --run-time 5m --headless

Pass/fail targets (§9.4):
    p95 latency (cache hit)     < 200 ms
    p95 latency (full LLM+DB)   < 4 s
    error rate                  < 1 %
    cache hit ratio (repeat)    > 70 %
    DB pool exhaustion errors   0
"""

import random
import uuid

from locust import HttpUser, between, task

TENANTS = ["tenant_a", "tenant_b", "tenant_c"]

QUESTIONS = [
    "Give me a summary of my store's task performance today.",
    "What tasks are at risk of becoming overdue in my district?",
    "Why are tasks being completed late in Store 118?",
    "I have 15 minutes left in my shift — what task can I knock out?",
]


class FieldAgentUser(HttpUser):
    wait_time = between(1, 4)  # think-time between questions

    def on_start(self):
        self.tenant_id = random.choice(TENANTS)
        self.session_id = str(uuid.uuid4())
        self.user_id = str(uuid.uuid4())

    @task(3)
    def ask_cached(self):
        # Repeat question → should hit Redis after the first miss
        self._ask("Give me a summary of my store's task performance today.")

    @task(1)
    def ask_random(self):
        # Varied → exercises LLM + DB
        self._ask(random.choice(QUESTIONS))

    def _ask(self, text):
        self.client.post(
            "/api/v1/ask",
            json={
                "text": text,
                "tenant_id": self.tenant_id,
                "session_id": self.session_id,
                "user_id": self.user_id,
                "agent_id": "field_user_agent",
            },
            name=text[:30],
        )
