"""Push workflows to the client's own n8n instance (n8n public REST API)."""
from __future__ import annotations

from app.channels import http
from app.security import check_public_url


def _headers(api_key: str) -> dict:
    return {"X-N8N-API-KEY": api_key, "Accept": "application/json"}


def verify(base_url: str, api_key: str) -> dict:
    check_public_url(base_url)
    r = http("GET", f"{base_url.rstrip('/')}/api/v1/workflows", params={"limit": 1}, headers=_headers(api_key))
    return {"ok": True, "sample": len(r.get("data", []))}


def create_workflow(base_url: str, api_key: str, workflow: dict) -> dict:
    check_public_url(base_url)
    body = {
        "name": workflow.get("name", "SAHM workflow"),
        "nodes": workflow.get("nodes", []),
        "connections": workflow.get("connections", {}),
        "settings": workflow.get("settings", {"executionOrder": "v1"}),
    }
    r = http("POST", f"{base_url.rstrip('/')}/api/v1/workflows", json=body, headers=_headers(api_key))
    return {"id": r.get("id"), "name": r.get("name")}
