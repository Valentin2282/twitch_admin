import time
import asyncio
import httpx
import google.generativeai as genai
from fastapi import FastAPI, Request

app = FastAPI()

TG_TOKEN = "8354796378:AAGBZMkEwvpJyCRdnALzbVo-b1n0NiFYBJY"
BOT_USERNAME = "@HATElove_ai"

# Оставляем только те модели, которые 100% работают и эффективны
WORKING_MODELS = ["gemini-1.5-flash", "gemini-1.5-pro"]

# Антиспам: словарь в памяти (сбрасывается при деплое/перезагрузке)
user_cooldowns = {}
COOLDOWN_SECONDS = 7

# Настройка API один раз при загрузке (экономит ресурсы)
# Выбираем ключ из списка, можно взять первый для стабильности
import random
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
genai.configure(api_key=random.choice(API_KEYS))

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
    
    # ФИЛЬТРЫ
    bot_raw_username = BOT_USERNAME.replace("@", "")
    is_private = msg["chat"]["type"] == "private"
    is_reply = msg.get("reply_to_message", {}).get("from", {}).get("username") == bot_raw_username
    is_mentioned = BOT_USERNAME.lower() in user_text.lower()
    
    if not (is_private or is_reply or is_mentioned):
        return {"status": "not_for_me"}
        
    # АНТИСПАМ
    now = time.time()
    if now - user_cooldowns.get(user_id, 0) < COOLDOWN_SECONDS:
        return {"status": "rate_limited"}
    user_cooldowns[user_id] = now
    
    # ДЕЙСТВИЕ
    await send_tg_chat_action(chat_id)
    
    # ГЕНЕРАЦИЯ (без лишних циклов)
    clean_text = user_text.replace(BOT_USERNAME, "").strip()
    
    for model_name in WORKING_MODELS:
        try:
            model = genai.GenerativeModel(model_name)
            response = await asyncio.wait_for(
                model.generate_content_async(f"Ты участник чата. Ответь: {clean_text}"),
                timeout=7.0
            )
            await send_tg_message(chat_id, response.text)
            return {"status": "ok"}
        except Exception:
            continue 
            
    await send_tg_message(chat_id, "Техническая пауза, попробуй через пару секунд.")
    return {"status": "error"}
