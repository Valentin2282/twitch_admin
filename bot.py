import time
import random
import asyncio
import httpx
import google.generativeai as genai
from fastapi import FastAPI, Request

# Инициализируем отдельное приложение для бота
app = FastAPI()

TG_TOKEN = "8354796378:AAGBZMkEwvpJyCRdnALzbVo-b1n0NiFYBJY"
BOT_USERNAME = "@HATElove_ai"  # Юзернейм для фильтрации в общих чатах

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

# Словарик для антиспама (хранится в памяти Vercel между "теплыми" стартами)
user_cooldowns = {}
COOLDOWN_SECONDS = 7  # Сколько секунд игнорировать юзера после его запроса

async def send_tg_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})

async def send_tg_chat_action(chat_id: int, action: str = "typing"):
    """Отправляет статус 'печатает...' корректно ожидая завершения (без краша Vercel)"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": chat_id, "action": action})

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    # 1. ЗАЩИТА: Проверяем, что это именно текстовое сообщение
    if "message" not in data or "text" not in data["message"]:
        return {"status": "ignored"}
        
    msg = data["message"]
    chat_id = msg["chat"]["id"]
    chat_type = msg["chat"]["type"]
    user_id = msg["from"]["id"]
    user_text = msg["text"]
    
    # 2. ФИЛЬТР ДЛЯ ОБЩИХ ЧАТОВ
    is_private = chat_type == "private"
    bot_raw_username = BOT_USERNAME.replace("@", "")
    
    # Был ли реплай на сообщение нашего бота?
    is_reply_to_bot = msg.get("reply_to_message", {}).get("from", {}).get("username") == bot_raw_username
    # Упомянули ли бота по юзернейму в тексте?
    is_mentioned = BOT_USERNAME.lower() in user_text.lower()
    
    # Если это группа, и нас не тегали и не реплаили — молча уходим
    if not (is_private or is_reply_to_bot or is_mentioned):
        return {"status": "not_for_me"}
        
    # 3. АНТИСПАМ (Rate Limiting)
    now = time.time()
    last_time = user_cooldowns.get(user_id, 0)
    
    if now - last_time < COOLDOWN_SECONDS:
        # Человек спамит — вежливо осаживаем его
        await send_tg_message(chat_id, "Воу, полегче! Я не успеваю, дай пару секунд передохнуть.")
        return {"status": "rate_limited"}
        
    # Запоминаем время последнего успешного запроса юзера
    user_cooldowns[user_id] = now
    
    # 4. ВИДИМОСТЬ: Включаем статус "печатает"
    await send_tg_chat_action(chat_id)
    
    # Чистим текст от юзернейма бота, чтобы нейронка не пыталась отвечать на свой же ник
    clean_text = user_text.replace(BOT_USERNAME, "").strip()
    
    # 5. ГЕНЕРАЦИЯ ОТВЕТА (Оптимизированная очередь)
    current_key = random.choice(API_KEYS)
    genai.configure(api_key=current_key)
    success = False
    
    for model_name in FALLBACK_MODELS:
        try:
            model = genai.GenerativeModel(model_name)
            # Жесткий таймаут 7 секунд. Если модель тупит дольше — Vercel убьет функцию (лимит 10-15 сек).
            # Поэтому мы сами обрываем долгий запрос и моментально берем следующую модель.
            response = await asyncio.wait_for(
                model.generate_content_async(f"Ты — участник чата. Ответь коротко: {clean_text}"),
                timeout=7.0
            )
            await send_tg_message(chat_id, response.text)
            success = True
            break 
            
        except asyncio.TimeoutError:
            continue 
        except Exception:
            continue 
            
    # Если мы перебрали все модели, и ни одна не ответила за 7 секунд
    if not success:
        await send_tg_message(chat_id, "Чет я подвис, давай попозже.")

    # Телеграм всегда должен получать 200 OK, чтобы не создавать очередь
    return {"status": "ok"}
