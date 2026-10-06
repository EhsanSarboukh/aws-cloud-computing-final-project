import os
import socket
import time
import datetime

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("ALLOWED_ORIGIN", "*")],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Points at visits-service's PRIVATE address (e.g. http://<private-ip>:8080)
# — this call never goes through the ALB, it's instance-to-instance traffic
# inside the VPC, allowed only because stats-service's security group is
# explicitly permitted to reach visits-service's security group on 8080.
VISITS_SERVICE_URL = os.environ["VISITS_SERVICE_URL"]

START_TIME = time.time()


@app.get("/health")
def health():
    """Target group health check hits this directly, bypassing ALB routing
    rules entirely, so it doesn't need the /stats path prefix."""
    return {"status": "ok", "service": "stats-service", "hostname": socket.gethostname()}


@app.get("/stats")
def stats():
    """Calls visits-service internally, then adds a derived metric on top —
    this is the one microservice-to-microservice call in the whole project.
    Path starts with /stats so the ALB's listener rule can route it here."""
    resp = httpx.get(f"{VISITS_SERVICE_URL}/visits/count", timeout=5.0)
    resp.raise_for_status()
    total_visits = resp.json()["total_visits"]

    uptime_minutes = max((time.time() - START_TIME) / 60, 0.01)

    return {
        "total_visits": total_visits,
        "visits_per_minute_since_stats_started": round(total_visits / uptime_minutes, 2),
        "handled_by_hostname": socket.gethostname(),
        "upstream_service": "visits-service (called internally, not through the ALB)",
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }
# redeploy trigger 1791306241
