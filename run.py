"""Entry point that reads the port from the environment in Python, so it never
depends on shell expansion of $PORT (which some Railway start modes don't do)."""
import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
