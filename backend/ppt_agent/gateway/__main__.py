from __future__ import annotations

import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run("ppt_agent.gateway.app:app", host="127.0.0.1", port=int(os.environ.get("PPT_AGENT_PORT", "8765")), log_level="info")
