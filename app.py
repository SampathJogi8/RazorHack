"""
app.py — Top-level ASGI Entrypoint for Vercel, Uvicorn, and Production Serverless Deployments.
---------------------------------------------------------------------------------------------
This file exposes the top-level "app" FastAPI instance expected by Vercel's Python runtime.
For the Streamlit dashboard, run:
    streamlit run streamlit_app.py
"""

from api import app

# Top-level ASGI FastAPI instance
__all__ = ["app"]
