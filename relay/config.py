"""Engine configuration. Every value can be overridden with an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    db_path: str = str(ROOT / "relay.db")
    data_dir: Path = ROOT / "data"
    world_url: str = "http://localhost:9210"      # the mock world
    demo_token: str = "demo-token"                 # bearer token for platform APIs ("" disables auth)
    decider: str = "demo-user"                     # recorded as decided_by (single demo token)
    lease_seconds: float = 10.0                    # a crashed worker's job is re-claimable after this
    poll_interval: float = 0.3
    http_timeout: float = 10.0                     # every mock-world call
    ai_timeout: float = 30.0                       # every model-provider call
    max_attempts: int = 6                          # per step, for transient failures (~15 s of outage tolerated)
    backoff_base: float = 0.5                      # seconds; doubles each retry
    backoff_max: float = 8.0                       # cap on a single wait
    default_max_steps: int = 100                   # used when a definition has no limits.max_steps
    max_job_deliveries: int = 5                    # a run that crashes workers this often is failed
    provider: str = "heuristic"                    # heuristic | anthropic | openai

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        s = cls()
        s.db_path = e("RELAY_DB", s.db_path)
        s.data_dir = Path(e("RELAY_DATA_DIR", str(s.data_dir)))
        s.world_url = e("MOCK_WORLD_URL", s.world_url).rstrip("/")
        s.demo_token = e("RELAY_DEMO_TOKEN", s.demo_token)
        s.lease_seconds = float(e("RELAY_LEASE_SECONDS", s.lease_seconds))
        s.http_timeout = float(e("RELAY_HTTP_TIMEOUT", s.http_timeout))
        s.ai_timeout = float(e("RELAY_AI_TIMEOUT", s.ai_timeout))
        s.provider = e("RELAY_PROVIDER", s.provider)
        return s
