from dotenv import load_dotenv
load_dotenv()  # must run before importing anything that reads env vars at import time (e.g. database.py)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import init_db
from routes_dashboard import router as dashboard_router
from routes_webhook import router as webhook_router

app = FastAPI(title="Sales Command Center API")

# Wide open for now since the dashboard is an internal office TV/desktop tool.
# Tighten allow_origins to your actual frontend domain once deployed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()


app.include_router(dashboard_router)
app.include_router(webhook_router)


@app.get("/health")
def health():
    return {"status": "ok"}
