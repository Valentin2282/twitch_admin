import random
import asyncio

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

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        user_text = data["message"]["text"]
        
        # В serverless (Vercel) глобальный счетчик currentKeyIdx = 0 сбрасывается 
        # при каждом "холодном" старте, поэтому random.choice работает стабильнее
        current_key = random.choice(API_KEYS)
        genai.configure(api_key=current_key)
        
        success = False
        
        # Пытаемся получить ответ, перебирая модели
        for model_name in FALLBACK_MODELS:
            try:
                model = genai.GenerativeModel(model_name)
                
                # Ставим жесткий таймаут 8 секунд. Если модель тупит, 
                # мы успеем корректно ответить Телеграму и Vercel не упадет.
                response = await asyncio.wait_for(
                    model.generate_content_async(f"Ты — участник чата. Ответь коротко: {user_text}"),
                    timeout=8.0
                )
                
                await send_tg_message(chat_id, response.text)
                success = True
                break # Успех, выходим из цикла
                
            except asyncio.TimeoutError:
                continue # Таймаут, пробуем следующую модель
            except Exception as e:
                continue # Ошибка API (например, модели еще не существует), идем дальше
                
        if not success:
            # Если все модели упали или отвалились по таймауту, спасаем бота
            await send_tg_message(chat_id, "Чет я подвис, давай попозже.")

    # Телеграм обязан получить этот ответ максимально быстро
    return {"status": "ok"}
