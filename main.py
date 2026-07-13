import os
import httpx
import jwt
import pathlib # Добавили библиотеку для работы с файлами
import time
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Request, Response, Depends, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, PlainTextResponse # Добавили HTMLResponse и PlainTextResponse
from pydantic import BaseModel
from typing import Optional

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

# --- РОУТ ДЛЯ ОТДАЧИ НОВОЙ СТРАНИЦЫ НАГРАД ---
@app.get("/rewards", response_class=HTMLResponse)
async def rewards_page(request: Request):
    token = request.cookies.get("admin_session")
    if not token: 
        return RedirectResponse(url="/")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
        twitch_login = payload.get('login', 'Admin')
        html_content = get_html("rewards.html").replace("{{USERNAME}}", twitch_login)
        return HTMLResponse(content=html_content)
    except jwt.PyJWTError:
        return RedirectResponse(url="/")

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

# --- 6.1 БАЗА ИГРОКОВ (ТАБЛИЦА С ПОИСКОМ И СОРТИРОВКОЙ) ---
@app.get("/api/v1/admin/users")
async def get_admin_users(
    request: Request,
    search: str = "",
    hasTwitch: str = "all",
    trust: str = "all",
    sort_by: str = "total_message_count", # <-- Ловим параметр из JS
    supabase: httpx.AsyncClient = Depends(get_supabase_client)
):
    token = request.cookies.get("admin_session")
    if not token: raise HTTPException(status_code=401)
    
    try:
        # Безопасный словарь доступных сортировок (защита от инъекций)
        allowed_sorts = {
            "total_message_count": "total_message_count.desc",
            "weekly_message_count": "weekly_message_count.desc",
            "monthly_message_count": "monthly_message_count.desc",
            "telegram_total_message_count": "telegram_total_message_count.desc",
            "coins": "coins.desc",
            "tickets": "tickets.desc"
        }
        
        # Если пришел мусор, скидываем на сортировку по умолчанию
        order_param = allowed_sorts.get(sort_by, "total_message_count.desc")

        params = {
            "select": "telegram_id,full_name,photo_url,twitch_login,telegram_total_message_count,total_message_count,coins,tickets,trust_level",
            "order": order_param,
            "limit": "100" # Забираем топ-100 по выбранному критерию
        }
        
        # Фильтр по Твичу
        if hasTwitch == "linked":
            params["twitch_login"] = "not.is.null"
            
        # Фильтр по Трасту
        if trust != "all":
            params["trust_level"] = f"eq.{trust}"

        # Поиск
        if search:
            search_clean = search.strip()
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
class RewardCreateRequest(BaseModel):
    title: str
    reward_type: str
    steam_item_name: Optional[str] = ""
    auto_steam: Optional[bool] = False
    reward_amount: Optional[int] = 10
    target_value: Optional[int] = 0
    notify_admin: Optional[bool] = True
    show_user_input: Optional[bool] = True

# --- 8. НАСТРОЙКА УНИКАЛЬНЫХ НАГРАД TWITCH ---

# --- ОБНОВЛЕННЫЙ ЭНДПОИНТ НАГРАД С АВТО-РЕЗОЛВОМ ИМЕН СТРИМЕРОВ ---
@app.get("/api/v1/admin/rewards")
async def get_admin_rewards_panel(request: Request):
    token = request.cookies.get("admin_session")
    if not token: raise HTTPException(status_code=401)
    try:
        jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401)

    supabase = get_resilient_supabase()
    client_id = os.getenv("TWITCH_CLIENT_ID")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET")

    try:
        broadcaster_ids = get_allowed_ids()
        channels_metadata = []

        # Получаем App Access Token для запроса к Helix API
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                "https://id.twitch.tv/oauth2/token",
                data={"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"}
            )
            if token_resp.status_code == 200:
                app_token = token_resp.json()["access_token"]
                
                # Запрашиваем данные профилей всех стримеров из белого списка за один раз
                ids_params = [("id", b_id) for b_id in broadcaster_ids]
                tw_res = await client.get(
                    "https://api.twitch.tv/helix/users",
                    headers={"Client-ID": client_id, "Authorization": f"Bearer {app_token}"},
                    params=ids_params
                )
                if tw_res.status_code == 200:
                    for u_data in tw_res.json().get("data", []):
                        channels_metadata.append({
                            "id": u_data["id"],
                            "login": u_data["login"],
                            "display_name": u_data["display_name"],
                            "profile_image": u_data["profile_image_url"]
                        })

        # Если Twitch упал, делаем fallback на ID
        if not channels_metadata:
            channels_metadata = [{"id": b_id, "login": f"Channel_{b_id}", "display_name": f"ID: {b_id}", "profile_image": ""} for b_id in broadcaster_ids]

        # Подтягиваем список триггеров наград
        res_rewards = await supabase.get("/rest/v1/twitch_rewards", params={"order": "id.desc"})
        rewards = res_rewards.json() if res_rewards.status_code == 200 else []

        # Подтягиваем лог отчетности по команде !подарок (последние 50 вызовов)
        res_logs = await supabase.get("/rest/v1/gift_logs", params={"order": "id.desc", "limit": "50"})
        gift_logs = res_logs.json() if res_logs.status_code == 200 else []

        return {
            "channels": channels_metadata, 
            "rewards": rewards,
            "gift_logs": gift_logs
        }
    except Exception as e:
        return {"channels": [], "rewards": [], "gift_logs": []}

@app.post("/api/v1/admin/rewards/create")
async def create_admin_twitch_reward(req: RewardCreateRequest, request: Request):
    token = request.cookies.get("admin_session")
    if not token: raise HTTPException(status_code=401)
    
    supabase = get_resilient_supabase()
    try:
        payload = {
            "title": req.title,
            "reward_type": req.reward_type,
            "steam_item_name": req.steam_item_name,
            "auto_steam": req.auto_steam,
            "reward_amount": req.reward_amount,
            "promocode_amount": req.reward_amount, # Синхронизация для обратной совместимости
            "condition_type": "twitch_messages_session" if req.target_value > 0 else "none",
            "target_value": req.target_value,
            "notify_admin": req.notify_admin,
            "show_user_input": req.show_user_input, # Флаг возврата баллов / инпута
            "is_active": True
        }
        
        res = await supabase.post("/rest/v1/twitch_rewards", json=payload)
        if res.status_code in [200, 201, 204]:
            return {"status": "success"}
        raise HTTPException(status_code=400, detail=res.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/admin/rewards/toggle")
async def toggle_admin_twitch_reward(id: int, status: bool, request: Request):
    token = request.cookies.get("admin_session")
    if not token: raise HTTPException(status_code=401)
    
    supabase = get_resilient_supabase()
    try:
        await supabase.patch("/rest/v1/twitch_rewards", params={"id": f"eq.{id}"}, json={"is_active": status})
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error"}

# --- РОУТ ДЛЯ ОТДАЧИ СТРАНИЦЫ КОРОБОК ---
@app.get("/boxes", response_class=HTMLResponse)
async def boxes_page(request: Request):
    token = request.cookies.get("admin_session")
    if not token: 
        return RedirectResponse(url="/")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
        twitch_login = payload.get('login', 'Admin')
        html_content = get_html("boxes.html").replace("{{USERNAME}}", twitch_login)
        return HTMLResponse(content=html_content)
    except jwt.PyJWTError:
        return RedirectResponse(url="/")

# --- СХЕМА СОЗДАНИЯ КОРОБКИ ---
class BoxCreateRequest(BaseModel):
    name: str
    box_type: str = "nick_length"

# --- ЭНДПОИНТЫ ДЛЯ РАБОТЫ С КОРОБКАМИ ---
@app.get("/api/v1/admin/boxes")
async def get_admin_boxes(request: Request):
    token = request.cookies.get("admin_session")
    if not token: raise HTTPException(status_code=401)
    
    supabase = get_resilient_supabase()
    try:
        # Запрашиваем коробки и сразу считаем, сколько внутри предметов
        res = await supabase.get("/rest/v1/reward_boxes", params={"select": "*,items:reward_box_items(count)", "order": "id.desc"})
        return res.json() if res.status_code == 200 else []
    except Exception:
        return []

@app.post("/api/v1/admin/boxes/create")
async def create_admin_box(req: BoxCreateRequest, request: Request):
    token = request.cookies.get("admin_session")
    if not token: raise HTTPException(status_code=401)
    
    supabase = get_resilient_supabase()
    try:
        res = await supabase.post("/rest/v1/reward_boxes", json={"name": req.name, "box_type": req.box_type})
        if res.status_code in [200, 201, 204]: return {"status": "ok"}
        raise HTTPException(status_code=400, detail="Ошибка создания")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/admin/boxes/{box_id}/generate")
async def generate_box_content(box_id: int, request: Request):
    """Генерация 30 слотов (для ников от 1 до 30 символов) рандомными скинами из cs_items"""
    token = request.cookies.get("admin_session")
    if not token: raise HTTPException(status_code=401)
    
    supabase = get_resilient_supabase()
    try:
        # 1. Берем 100 активных скинов из базы магазина
        items_res = await supabase.get("/rest/v1/cs_items", params={"is_active": "eq.true", "limit": "100"})
        available_items = items_res.json()
        if not available_items:
            raise HTTPException(status_code=400, detail="В таблице cs_items нет активных скинов для генерации!")

        import random
        random.shuffle(available_items)
        
        box_items_payload = []
        # Генерируем 30 слотов (максимальная длина логина Twitch ~25 символов)
        for i in range(1, 31):
            item = random.choice(available_items)
            skin_name = item.get("name") or item.get("market_hash_name", "Секретный скин")
            box_items_payload.append({
                "box_id": box_id,
                "slot_index": i,
                "skin_name": skin_name,
                "chance_weight": 10
            })
            
        # 2. Очищаем старые предметы этой коробки
        await supabase.delete("/rest/v1/reward_box_items", params={"box_id": f"eq.{box_id}"})
        
        # 3. Заливаем новые
        await supabase.post("/rest/v1/reward_box_items", json=box_items_payload)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
