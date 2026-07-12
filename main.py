from fastapi import FastAPI

app = FastAPI(title="Stream Panel API")

@app.get("/")
async def root():
    return {
        "status": "ok", 
        "message": "Стримовский инстанс успешно запущен и готов к работе!"
    }

@app.get("/api/v1/ping")
async def ping():
    return {"ping": "pong"}
