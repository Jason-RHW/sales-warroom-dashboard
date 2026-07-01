from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes_dashboard import router as dashboard_router
from routes_webhook import router as webhook_router

app = FastAPI(title="Sales Command Center API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(dashboard_router)
app.include_router(webhook_router)


@app.get("/health")
def health():
    return {"status": "ok"}
