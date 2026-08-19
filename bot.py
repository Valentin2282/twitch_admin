import random
import asyncio
import httpx
import google.generativeai as genai
from fastapi import FastAPI, Request

# Инициализируем отдельное приложение для бота
app = FastAPI()

TG_TOKEN = "8354796378:AAGBZMkEwvpJyCRdnALzbVo-b1n0NiFYBJY"

API_KEYS = [
    "AIzaSyCTA0JmbppVzWBEK8okOXSSaljdkK02jBc",
    "AIzaSyDB89SLS76uT-yPGCAXlUGzdL3IHiLsMvI",
    "AIzaSyCJnthrKjjZbMddkJGA8EFp9v9_fFLGgAw",
    "AIzaSyAayhXt8froc_vybN47D_F3VIRGOuhC9ik",
    "AIzaSyAWUo0Te5f5Ek4TM7oWfoGXUEe8eQvmu8w",
    "AIzaSyAHZzxeN1bNyHFRL7UsmJIZ7OPYQTh5i-Q",
    "AIzaSyB9vbQqDXtTiX6D0ykp44TZ3hr9lMCFmQE",
    "AIzaSyAAtKmVcls9bY7i_Gv6Y-hVUlOoHVgIZb4",
    "AIzaSyDhi5SzypGmqVM9r0A-zmMW6AwMeScUy9E"
]

FALLBACK_MODELS = [
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-tts-preview",
    "gemini-2.0-flash",
    "gemini-1.5-flash"
]

async def send_tg_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        user_text = data["message"]["text"]
        
        current_key = random.choice(API_KEYS)
        genai.configure(api_key=current_key)
        success = False
        
        for model_name in FALLBACK_MODELS:
            try:
                model = genai.GenerativeModel(model_name)
                response = await asyncio.wait_for(
                    model.generate_content_async(f"Ты — участник чата. Ответь коротко: {user_text}"),
                    timeout=8.0
                )
                await send_tg_message(chat_id, response.text)
                success = True
                break 
            except asyncio.TimeoutError:
                continue 
            except Exception:
                continue 
                
        if not success:
            await send_tg_message(chat_id, "Чет я подвис, давай попозже.")

    return {"status": "ok"}
