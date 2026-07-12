import os
import httpx
import jwt
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

app = FastAPI(title="Stream Admin Panel")

# --- ХЕЛПЕРЫ ДЛЯ ДИНАМИЧЕСКОГО ЗАПРОСА ENV (ВЕРСИЯ ДЛЯ VERCEL) ---
def get_jwt_secret():
    return os.getenv("JWT_SECRET", "super_secret_fallback_key")

def get_allowed_ids():
    raw_ids = os.getenv("TWITCH_BROADCASTER_ID", "883996654,755238101")
    return [x.strip() for x in raw_ids.split(",") if x.strip()]

def get_redirect_uri():
    # Хардкод или тоже можно вынести в ENV, если захочешь менять домен
    return "https://twitch-admin.vercel.app/api/v1/auth/callback"

def create_jwt_token(data: dict):
    expire = datetime.now(timezone.utc) + timedelta(days=7) # Сессия на 7 дней
    to_encode = data.copy()
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, get_jwt_secret(), algorithm="HS256")

# --- 1. ГЛАВНАЯ СТРАНИЦА (С ПРОВЕРКОЙ ДОСТУПА) ---
@app.get("/")
async def root(request: Request):
    # Пытаемся прочитать токен из куки
    token = request.cookies.get("admin_session")
    
    if not token:
        return {
            "status": "unauthorized", 
            "message": "Вы не авторизованы. Перейдите по ссылке для входа.", 
            "login_url": "/api/v1/auth/login"
        }
        
    try:
        # Расшифровываем токен (секрет запрашивается динамически)
        payload = jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
        return {
            "status": "success", 
            "message": f"Добро пожаловать в защищенную панель, {payload.get('login')}!", 
            "user": payload,
            "logout_url": "/api/v1/auth/logout"
        }
    except jwt.PyJWTError:
        # Если токен подделан или истек
        return {
            "status": "unauthorized", 
            "message": "Сессия истекла или недействительна.", 
            "login_url": "/api/v1/auth/login"
        }

# --- 2. КНОПКА ВОЙТИ (РЕДИРЕКТ НА TWITCH) ---
@app.get("/api/v1/auth/login")
async def login():
    # Запрашиваем ID клиента прямо перед редиректом
    client_id = os.getenv("TWITCH_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=500, detail="TWITCH_CLIENT_ID не настроен в Vercel")

    url = (
        f"https://id.twitch.tv/oauth2/authorize?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={get_redirect_uri()}"
        f"&scope=user:read:email"
    )
    return RedirectResponse(url)

# --- 3. ОБРАБОТКА ВОЗВРАТА ОТ TWITCH ---
@app.get("/api/v1/auth/callback")
async def auth_callback(code: str, response: Response):
    # Запрашиваем ключи динамически
    client_id = os.getenv("TWITCH_CLIENT_ID")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Ключи Twitch не настроены в Vercel")

    # 1. Меняем временный код на токен доступа Twitch
    token_url = "https://id.twitch.tv/oauth2/token"
    token_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": get_redirect_uri()
    }
    
    async with httpx.AsyncClient() as client:
        token_res = await client.post(token_url, data=token_data)
        if token_res.status_code != 200:
            raise HTTPException(status_code=400, detail="Ошибка обмена кода от Twitch")
            
        access_token = token_res.json().get("access_token")
        
        # 2. Узнаем, кто именно залогинился (запрос профиля)
        user_res = await client.get(
            "https://api.twitch.tv/helix/users",
            headers={"Authorization": f"Bearer {access_token}", "Client-Id": client_id}
        )
        if user_res.status_code != 200:
            raise HTTPException(status_code=400, detail="Ошибка получения профиля Twitch")
            
        user_data = user_res.json().get("data", [])[0]
        twitch_id = user_data.get("id")
        twitch_login = user_data.get("login")
        
        # 🔥 3. САМОЕ ВАЖНОЕ: ПРОВЕРКА НА АДМИНА (Динамический список) 🔥
        allowed_ids = get_allowed_ids()
        if twitch_id not in allowed_ids:
            raise HTTPException(status_code=403, detail=f"Доступ запрещен! Ваш ID ({twitch_id}) нет в белом списке.")
            
        # 4. Выдаем пропуск: создаем JWT-токен и кладем его в защищенную куку браузера
        jwt_token = create_jwt_token({"id": twitch_id, "login": twitch_login})
        
        # Отправляем обратно на главную страницу с новой кукой
        redirect = RedirectResponse(url="/")
        redirect.set_cookie(
            key="admin_session", 
            value=jwt_token, 
            httponly=True,  # Куку нельзя украсть через JavaScript (XSS защита)
            secure=True,    # Только HTTPS
            samesite="lax",
            max_age=7 * 24 * 3600 # 7 дней
        )
        return redirect

# --- 4. ВЫХОД ИЗ ПАНЕЛИ ---
@app.get("/api/v1/auth/logout")
async def logout():
    redirect = RedirectResponse(url="/")
    redirect.delete_cookie("admin_session") # Удаляем куку
    return redirect

# --- 5. 🛠️ РЕМОНТ ПОДПИСОК TWITCH (МУЛЬТИАККАУНТ) ---
@app.get("/api/v1/debug/fix_twitch_subs")
async def fix_twitch_subs(request: Request):
    # Защита: проверяем, что ты авторизован как админ
    token = request.cookies.get("admin_session")
    if not token:
        raise HTTPException(status_code=401, detail="Сначала войдите в панель!")
    try:
        jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Сессия недействительна.")

    # Динамический сбор ключей из Vercel ENV
    client_id = os.getenv("TWITCH_CLIENT_ID")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET")
    webhook_secret = os.getenv("TWITCH_WEBHOOK_SECRET")
    web_app_url = os.getenv("WEB_APP_URL")

    if not all([client_id, client_secret, webhook_secret, web_app_url]):
         return {"error": "Отсутствуют переменные окружения в Vercel (TWITCH ключи, WEBHOOK_SECRET или WEB_APP_URL)"}

    async with httpx.AsyncClient() as client:
        # 1. Получаем токен авторизации приложения
        token_resp = await client.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials"
            }
        )
        if token_resp.status_code != 200:
            return {"error": "Twitch Auth Failed", "details": token_resp.json()}
        
        access_token = token_resp.json()["access_token"]
        headers = {
            "Client-ID": client_id,
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        # 2. Берем ID стримеров
        broadcaster_ids = get_allowed_ids()
        callback_url = f"{web_app_url}/api/v1/webhooks/twitch"

        # 3. Удаляем ВСЕ старые подписки
        subs_resp = await client.get("https://api.twitch.tv/helix/eventsub/subscriptions", headers=headers)
        if subs_resp.status_code == 200:
            for sub in subs_resp.json().get("data", []):
                if sub["status"] != "enabled" or callback_url in sub["transport"]["callback"]:
                    await client.delete(f"https://api.twitch.tv/helix/eventsub/subscriptions?id={sub['id']}", headers=headers)

        # 4. Создаем НОВЫЕ подписки для КАЖДОГО стримера
        event_types = [
            "channel.channel_points_custom_reward_redemption.add",
            "stream.online",
            "stream.offline"
        ]
        
        created_subs = []
        for b_id in broadcaster_ids:
            for event_type in event_types:
                sub_payload = {
                    "type": event_type,
                    "version": "1",
                    "condition": {"broadcaster_user_id": b_id},
                    "transport": {
                        "method": "webhook",
                        "callback": callback_url,
                        "secret": webhook_secret
                    }
                }
                create_resp = await client.post("https://api.twitch.tv/helix/eventsub/subscriptions", headers=headers, json=sub_payload)
                created_subs.append({f"Channel {b_id} - {event_type}": create_resp.status_code})

        return {
            "message": "Подписки успешно обновлены для всех каналов!",
            "target_webhook": callback_url,
            "results": created_subs
        }
