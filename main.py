import os
import httpx
import jwt
import pathlib # Добавили библиотеку для работы с файлами
import time
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Request, Response, Depends, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, PlainTextResponse # Добавили HTMLResponse и PlainTextResponse

app = FastAPI(title="Stream Admin Panel")

# --- ХЕЛПЕРЫ ДЛЯ ДИНАМИЧЕСКОГО ЗАПРОСА ENV (ВЕРСИЯ ДЛЯ VERCEL) ---
def get_jwt_secret():
    return os.getenv("JWT_SECRET", "super_secret_fallback_key")

def get_allowed_ids():
    raw_ids = os.getenv("TWITCH_BROADCASTER_ID", "883996654,755238101")
    return [x.strip() for x in raw_ids.split(",") if x.strip()]

def get_redirect_uri():
    return "https://twitch-admin.vercel.app/api/v1/auth/callback"

def create_jwt_token(data: dict):
    expire = datetime.now(timezone.utc) + timedelta(days=7)
    to_encode = data.copy()
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, get_jwt_secret(), algorithm="HS256")

# Функция для чтения HTML файлов
def get_html(filename: str) -> str:
    path = pathlib.Path(__file__).parent / filename
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"<h1>Ошибка: файл {filename} не найден!</h1>"

# --- 1. ГЛАВНАЯ СТРАНИЦА (ЛОГИН ИЛИ РЕДИРЕКТ НА НАСТРОЙКИ) ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    token = request.cookies.get("admin_session")
    if token:
        try:
            # Если токен есть и он валиден — сразу кидаем в админку
            jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
            return RedirectResponse(url="/settings")
        except jwt.PyJWTError:
            pass # Если токен протух, продолжаем и показываем логин
            
    # Отдаем красивую страницу входа
    return HTMLResponse(content=get_html("main.html"))

# --- 1.5. ПАНЕЛЬ УПРАВЛЕНИЯ (ЗАЩИЩЕННАЯ ЗОНА) ---
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    token = request.cookies.get("admin_session")
    if not token:
        # Если пришел без пропуска - выкидываем на логин
        return RedirectResponse(url="/")
        
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
        twitch_login = payload.get('login', 'Admin')
        
        # Читаем HTML и заменяем переменную {{USERNAME}} на реальный ник
        html_content = get_html("settings.html").replace("{{USERNAME}}", twitch_login)
        return HTMLResponse(content=html_content)
        
    except jwt.PyJWTError:
        return RedirectResponse(url="/")

# --- 2. КНОПКА ВОЙТИ (РЕДИРЕКТ НА TWITCH) ---
@app.get("/api/v1/auth/login")
async def login():
    # Запрашиваем ID клиента прямо перед редиректом
    client_id = os.getenv("TWITCH_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=500, detail="TWITCH_CLIENT_ID не настроен в Vercel")

    # 🔥 ФИКС: Добавили scope channel:read:redemptions для вебхуков наград
    url = (
        f"https://id.twitch.tv/oauth2/authorize?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={get_redirect_uri()}"
        f"&scope=user:read:email+channel:read:redemptions"
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
        redirect = RedirectResponse(url="/settings")
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

# --- ХЕЛПЕР ДЛЯ SUPABASE ---
async def get_supabase_client():
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    
    if not supabase_url or not supabase_key:
        raise HTTPException(status_code=500, detail="Ключи Supabase не настроены")
        
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}"
    }
    # Возвращаем асинхронный клиент
    client = httpx.AsyncClient(base_url=supabase_url, headers=headers)
    try:
        yield client
    finally:
        await client.aclose()

# --- 6. ГЛОБАЛЬНАЯ СТАТИСТИКА (ДАШБОРД) ---
@app.get("/api/v1/admin/stats")
async def get_admin_dashboard_stats(
    request: Request,
    supabase: httpx.AsyncClient = Depends(get_supabase_client)
):
    token = request.cookies.get("admin_session")
    if not token: raise HTTPException(status_code=401)
    
    try:
        # Вызываем созданную RPC функцию в Supabase
        res = await supabase.post("/rest/v1/rpc/get_global_metrics", json={})
        if res.status_code != 200:
            raise Exception("Ошибка вызова RPC")
            
        return res.json() # База сразу вернет готовый агрегированный JSON
    except Exception as e:
        return {"error": str(e)}

# --- 6.1 БАЗА ИГРОКОВ (ТАБЛИЦА С ПОИСКОМ) ---
@app.get("/api/v1/admin/users")
async def get_admin_users(
    request: Request,
    search: str = "",
    hasTwitch: str = "all",
    trust: str = "all",
    supabase: httpx.AsyncClient = Depends(get_supabase_client)
):
    token = request.cookies.get("admin_session")
    if not token: raise HTTPException(status_code=401)
    
    try:
        # Формируем параметры запроса
        params = {
            "select": "telegram_id,full_name,photo_url,twitch_login,telegram_total_message_count,total_message_count,coins,tickets,trust_level",
            "order": "total_message_count.desc",
            "limit": "100" # Выводим топ-100 по фильтрам, чтобы не грузить браузер
        }
        
        # Применяем фильтр по Твичу
        if hasTwitch == "linked":
            params["twitch_login"] = "not.is.null"
            
        # Применяем фильтр по Трасту
        if trust != "all":
            params["trust_level"] = f"eq.{trust}"

        # Поиск (по TG ID, Имени или Логину Твича)
        if search:
            search_clean = search.strip()
            # or=(twitch_login.ilike.*search*,full_name.ilike.*search*,telegram_id.eq.search)
            if search_clean.isdigit():
                params["telegram_id"] = f"eq.{search_clean}"
            else:
                params["or"] = f"(twitch_login.ilike.*{search_clean}*,full_name.ilike.*{search_clean}*)"

        res = await supabase.get("/rest/v1/users", params=params)
        return res.json() if res.status_code == 200 else []
        
    except Exception as e:
        return []

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

# --- 7. 🔥 ВЫДЕЛЕННЫЙ ВЫСОКОСКОРОСТНОЙ БРОНЕЖИЛЕТ ДЛЯ ЧАТА (FOSSABOT_GUESS) ---
guess_cache = {
    "word": None,
    "is_active": False,
    "updated_at": 0,
    "raw_word": "",
    "cooldown_until": 0,
    "buffer_end": 0,
    "round_winners": []
}

# Два переиспользуемых глобальных клиента (минус TLS хэндшейки на каждый чих)
http_client = httpx.AsyncClient()
_resilient_supabase_client = None

def get_resilient_supabase():
    """Синглтон клиент для Supabase, сохраняющий коннекшн пул между вызовами Serverless"""
    global _resilient_supabase_client
    if _resilient_supabase_client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if url and key:
            headers = {"apikey": key, "Authorization": f"Bearer {key}"}
            _resilient_supabase_client = httpx.AsyncClient(base_url=url, headers=headers)
    return _resilient_supabase_client

@app.get("/api/v1/twitch/fossabot_guess", response_class=PlainTextResponse)
async def handle_fossabot_guess(
    request: Request,
    background_tasks: BackgroundTasks
):
    token = request.headers.get("x-fossabot-customapitoken") or request.query_params.get("token")
    if not token: 
        return ""

    # Достаем скоростной переиспользуемый клиент базы данных
    supabase = get_resilient_supabase()
    if not supabase:
        return ""

    try:
        global guess_cache
        now = time.time()

        # 1. Мгновенная блокировка гонки на уровне памяти
        if now > guess_cache.get("buffer_end", 0) and now < guess_cache.get("cooldown_until", 0):
            return ""

        # 2. Проверка 10-секундного лимита кэша с защитой от наплыва (Thundering Herd)
        if now - guess_cache["updated_at"] > 10:
            guess_cache["updated_at"] = now  # Запираем замок ДО await, блокируя параллельные запросы
            try:
                state_res = await supabase.get("/rest/v1/guess_state", params={"id": "eq.1"})
                if state_res.status_code == 200 and state_res.json():
                    state = state_res.json()[0]
                    guess_cache["raw_word"] = state.get("current_word", "") 
                    guess_cache["word"] = guess_cache["raw_word"].upper()
                    guess_cache["is_active"] = state.get("is_active", False)
            except Exception:
                guess_cache["updated_at"] = 0  # Сброс при сбое сети базы

        if not guess_cache["is_active"] or not guess_cache["word"]:
            return ""

        # 3. Запрос контекста сообщения в Fossabot API
        fb_res = await http_client.get(f"https://api.fossabot.com/v2/customapi/context/{token}", timeout=3.0)
        if fb_res.status_code != 200: 
            return ""
        message_data = fb_res.json().get("message")
        if not message_data: 
            return ""

        twitch_login = message_data["user"]["login"].lower()
        twitch_display = message_data["user"]["display_name"]
        guess_word = message_data["content"].strip().upper()

        # 4. Если слово не то — бот замолкает мгновенно
        if guess_word != guess_cache["word"]:
            return ""

        # --- ОБРАБОТКА ПОБЕДНОГО ВХОЖДЕНИЯ ---
        is_first_blood = False
        
        if now > guess_cache.get("cooldown_until", 0):
            guess_cache["buffer_end"] = now + 1.5       
            guess_cache["cooldown_until"] = now + 20    
            guess_cache["round_winners"] = []           
            is_first_blood = True
            
        # Начисление очков через фоновую задачу (эндпоинт не ждет ответа БД)
        if twitch_display not in guess_cache["round_winners"]:
            guess_cache["round_winners"].append(twitch_display)
            background_tasks.add_task(supabase.post, "/rest/v1/rpc/increment_guess_score", json={"p_twitch_login": twitch_login})

        if is_first_blood:
            target_filter = guess_cache["raw_word"]
            
            # Переносим тяжелый патч букв Supabase в бэкграунд-задачи Vercel. 
            # Бот выдаст текст в чат СРАЗУ, а база обновится через долю секунды сама.
            all_indices = list(range(len(target_filter)))
            background_tasks.add_task(
                supabase.patch, 
                "/rest/v1/guess_state", 
                params={"id": "eq.1"}, 
                json={"revealed_indices": all_indices}
            )

            winners_str = ", @".join(guess_cache["round_winners"])
            return f"🎉 Слово «{guess_cache['word']}» угадано! Очки забирают: @{winners_str}. След. слово через 20с."
        
        return ""

    except Exception:
        return ""
