import os
import time
import random
import asyncio
import httpx
from fastapi import FastAPI, Request
from google import genai

app = FastAPI()

# Теперь ключи безопасно тянутся из скрытых настроек Vercel!
TG_TOKEN = os.getenv("TG_BOT_TOKEN")
BOT_USERNAME = "@HATElove_ai"
WORKING_MODEL = "gemini-2.5-flash"
COOLDOWN_SECONDS = 7
user_cooldowns = {}

async def send_tg_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})

async def send_tg_chat_action(chat_id: int):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": chat_id, "action": "typing"})

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    if "message" not in data or "text" not in data["message"]:
        return {"status": "ignored"}
        
    msg = data["message"]
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    user_text = msg["text"]
    
    bot_raw_username = BOT_USERNAME.replace("@", "")
    is_private = msg["chat"]["type"] == "private"
    is_reply = msg.get("reply_to_message", {}).get("from", {}).get("username") == bot_raw_username
    is_mentioned = BOT_USERNAME.lower() in user_text.lower()
    
    if not (is_private or is_reply or is_mentioned):
        return {"status": "not_for_me"}
        
    now = time.time()
    if now - user_cooldowns.get(user_id, 0) < COOLDOWN_SECONDS:
        await send_tg_message(chat_id, "Воу, полегче! Я не успеваю, дай пару секунд передохнуть.")
        return {"status": "rate_limited"}
    user_cooldowns[user_id] = now
    
    await send_tg_chat_action(chat_id)
    clean_text = user_text.replace(BOT_USERNAME, "").strip()
    
    try:
        # Инициализация клиента по-новому
        current_key = random.choice(API_KEYS)
        client = genai.Client(api_key=current_key)
        
        # Асинхронный вызов в новом SDK выглядит иначе
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=WORKING_MODEL, 
                contents=f"Ты — участник чата. Ответь коротко: {clean_text}"
            ),
            timeout=7.0
        )
        await send_tg_message(chat_id, response.text)
        
    except asyncio.TimeoutError:
        await send_tg_message(chat_id, "Чет я подвис, давай попозже.")
    except Exception as e:
        # Если снова будет ошибка, бот честно скажет, какая именно
        await send_tg_message(chat_id, f"Сломался. Причина: {e}")

    return {"status": "ok"}
