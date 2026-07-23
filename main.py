import os
import httpx
import jwt
import pathlib
import time
import asyncio
import logging
import re
import base64
import urllib.parse
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Request, Response, Depends, BackgroundTasks, Query
from fastapi.responses import RedirectResponse, HTMLResponse, PlainTextResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Stream Admin Panel")

# ==============================================================================
# 🚀 1. ГЛОБАЛЬНЫЙ КЭШ И ПУЛ СОЕДИНЕНИЙ (СЕКРЕТ СКОРОСТИ VERCEL)
# ==============================================================================

# Читаем ENV ровно 1 раз при "холодном старте" Vercel
JWT_SECRET = os.getenv("JWT_SECRET", "super_secret_fallback_key")
TWITCH_BROADCASTER_ID = os.getenv("TWITCH_BROADCASTER_ID", "883996654,755238101")
ALLOWED_IDS = [x.strip() for x in TWITCH_BROADCASTER_ID.split(",") if x.strip()]
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
TWITCH_WEBHOOK_SECRET = os.getenv("TWITCH_WEBHOOK_SECRET", "")
WEB_APP_URL = os.getenv("WEB_APP_URL", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
QSTASH_TOKEN = os.getenv("QSTASH_TOKEN", "")

REDIRECT_URI = "https://twitch-admin.vercel.app/api/v1/auth/callback"

# Глобальные HTTP-клиенты с Keep-Alive. Vercel заморозит их в памяти.
# Это убирает ~200ms задержки на TLS-рукопожатие при каждом запросе!
http_limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
http_client = httpx.AsyncClient(limits=http_limits)

supabase_headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}"
} if SUPABASE_KEY else {}

supabase_client = httpx.AsyncClient(
    base_url=SUPABASE_URL,
    headers=supabase_headers,
    limits=http_limits
)

# Зависимость теперь просто отдает уже готовый, открытый коннект за O(1)
async def get_supabase_client():
    return supabase_client

# Кэшируем HTML файлы в оперативной памяти (Минус Disk I/O на фронтенде)
HTML_CACHE = {}

def get_html(filename: str) -> str:
    if filename not in HTML_CACHE:
        path = pathlib.Path(__file__).parent / filename
        try:
            with open(path, "r", encoding="utf-8") as f:
                HTML_CACHE[filename] = f.read()
        except FileNotFoundError:
            return f"<h1>Ошибка: файл {filename} не найден!</h1>"
    return HTML_CACHE[filename]

def create_jwt_token(data: dict):
    expire = datetime.now(timezone.utc) + timedelta(days=7)
    to_encode = data.copy()
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm="HS256")

# ==============================================================================
# 🌐 2. РОУТИНГ И АВТОРИЗАЦИЯ
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    token = request.cookies.get("admin_session")
    if token:
        try:
            jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            return RedirectResponse(url="/settings")
        except jwt.PyJWTError:
            pass
    return HTMLResponse(content=get_html("main.html"))

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    token = request.cookies.get("admin_session")
    if not token: return RedirectResponse(url="/")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return HTMLResponse(content=get_html("settings.html").replace("{{USERNAME}}", payload.get('login', 'Admin')))
    except jwt.PyJWTError:
        return RedirectResponse(url="/")

@app.get("/rewards", response_class=HTMLResponse)
async def rewards_page(request: Request):
    token = request.cookies.get("admin_session")
    if not token: return RedirectResponse(url="/")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return HTMLResponse(content=get_html("rewards.html").replace("{{USERNAME}}", payload.get('login', 'Admin')))
    except jwt.PyJWTError:
        return RedirectResponse(url="/")

@app.get("/boxes", response_class=HTMLResponse)
async def boxes_page(request: Request):
    token = request.cookies.get("admin_session")
    if not token: return RedirectResponse(url="/")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return HTMLResponse(content=get_html("boxes.html").replace("{{USERNAME}}", payload.get('login', 'Admin')))
    except jwt.PyJWTError:
        return RedirectResponse(url="/")

@app.get("/api/v1/auth/login")
async def login():
    if not TWITCH_CLIENT_ID:
        raise HTTPException(status_code=500, detail="TWITCH_CLIENT_ID не настроен")
    
    # Полный фарш прав для будущего функционала
    scopes_list = [
        "user:read:email",
        "user:write:chat",                # 🔥 НОВОЕ: Право писать в чат через Helix API
        "user:read:chat",                 # 🔥 НОВОЕ: Право читать чат через Helix API
        "channel:read:redemptions",
        "channel:manage:redemptions",     # Награды
        "channel:read:polls",
        "channel:manage:polls",           # Опросы
        "channel:read:predictions",
        "channel:manage:predictions",     # Прогнозы (ставки баллами)
        "channel:manage:broadcast",       # Управление названием и игрой стрима
        "channel:read:subscriptions",     # Чтение сабов
        "bits:read",                      # Чтение битсов
        "channel:moderate",               # Базовые права модератора
        "chat:read",                      # IRC чтение
        "chat:edit",                      # IRC отправка
        "moderator:manage:announcements", # Отправка /announce
        "moderator:manage:chat_messages", # Удаление сообщений
        "moderator:manage:banned_users",  # Бан/таймаут юзеров
        "channel:manage:schedule"         # Управление расписанием
    ]
    scopes = "+".join(scopes_list)
    
    url = (
        f"https://id.twitch.tv/oauth2/authorize?response_type=code"
        f"&client_id={TWITCH_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={scopes}"
    )
    return RedirectResponse(url)

@app.get("/api/v1/auth/callback")
async def auth_callback(code: str, response: Response):
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Ключи Twitch не настроены")

    token_data = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI
    }
    
    token_res = await http_client.post("https://id.twitch.tv/oauth2/token", data=token_data)
    if token_res.status_code != 200:
        raise HTTPException(status_code=400, detail="Ошибка обмена кода от Twitch")
        
    access_token = token_res.json().get("access_token")
    user_res = await http_client.get(
        "https://api.twitch.tv/helix/users",
        headers={"Authorization": f"Bearer {access_token}", "Client-Id": TWITCH_CLIENT_ID}
    )
    
    if user_res.status_code != 200:
        raise HTTPException(status_code=400, detail="Ошибка получения профиля Twitch")
        
    user_data = user_res.json().get("data", [])[0]
    twitch_id = user_data.get("id")
    
    if twitch_id not in ALLOWED_IDS:
        raise HTTPException(status_code=403, detail=f"Доступ запрещен! ID: {twitch_id}")
        
    jwt_token = create_jwt_token({"id": twitch_id, "login": user_data.get("login")})
    redirect = RedirectResponse(url="/settings")
    redirect.set_cookie(
        key="admin_session", value=jwt_token, httponly=True, secure=True, samesite="lax", max_age=604800
    )
    return redirect

BROADCASTER_REDIRECT_URI = "https://twitch-admin.vercel.app/api/v1/auth/broadcaster_callback"

@app.get("/api/v1/auth/broadcaster_login")
async def broadcaster_login():
    """Отправляет на Twitch для привязки второго аккаунта с принудительным запросом пароля"""
    if not TWITCH_CLIENT_ID:
        raise HTTPException(status_code=500, detail="TWITCH_CLIENT_ID не настроен")
        
    scopes_list = [
        "user:read:email",
        "user:write:chat",                # 🔥 НОВОЕ: Право писать в чат через Helix API
        "user:read:chat",                 # 🔥 НОВОЕ: Право читать чат через Helix API
        "channel:read:redemptions",
        "channel:manage:redemptions",
        "channel:read:polls",
        "channel:manage:polls",
        "channel:read:predictions",
        "channel:manage:predictions",
        "channel:manage:broadcast",
        "channel:read:subscriptions",
        "bits:read",
        "channel:moderate",
        "chat:read",
        "chat:edit",
        "moderator:manage:announcements",
        "moderator:manage:chat_messages",
        "moderator:manage:banned_users",
        "channel:manage:schedule"
    ]
    scopes = "+".join(scopes_list)
    
    url = (
        f"https://id.twitch.tv/oauth2/authorize?response_type=code"
        f"&client_id={TWITCH_CLIENT_ID}"
        f"&redirect_uri={BROADCASTER_REDIRECT_URI}"
        f"&scope={scopes}"
        f"&force_verify=true"
    )
    return RedirectResponse(url)

@app.get("/api/v1/auth/broadcaster_callback")
async def broadcaster_callback(
    code: Optional[str] = None, 
    error: Optional[str] = None, 
    error_description: Optional[str] = None
):
    if error or not code:
        error_msg = error_description or "Код авторизации не получен."
        return HTMLResponse(
            content=f"<h2 style='color:red;'>Сбой привязки канала</h2><p>{error_msg}</p>", 
            status_code=400
        )

    token_data = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": BROADCASTER_REDIRECT_URI
    }
    
    token_res = await http_client.post("https://id.twitch.tv/oauth2/token", data=token_data)
    if token_res.status_code != 200:
        return HTMLResponse(content=f"<h2 style='color:red;'>Ошибка обмена кода стримера</h2><p>{token_res.text}</p>")
        
    t_json = token_res.json()
    access_token = t_json.get("access_token")
    refresh_token = t_json.get("refresh_token")
    
    user_res = await http_client.get(
        "https://api.twitch.tv/helix/users",
        headers={"Authorization": f"Bearer {access_token}", "Client-Id": TWITCH_CLIENT_ID}
    )
    
    if user_res.status_code != 200:
        return HTMLResponse(content="<h2 style='color:red;'>Ошибка получения профиля канала</h2>")
        
    u_data = user_res.json().get("data", [])[0]
    twitch_id = u_data.get("id")
    twitch_login = u_data.get("login")
    
    # 🔥 ИЗМЕНЕНИЕ ЗДЕСЬ: Используем PATCH вместо POST
    db_res = await supabase_client.patch(
        "/rest/v1/users", 
        params={"twitch_id": f"eq.{twitch_id}"},
        json={
            "twitch_login": twitch_login,
            "twitch_access_token": access_token,
            "twitch_refresh_token": refresh_token
        },
        headers={"Prefer": "return=representation"} # Просим БД вернуть обновленную строку
    )
    
    # Если PATCH вернул пустой список [], значит такого twitch_id нет в таблице
    if db_res.status_code == 200 and len(db_res.json()) == 0:
        return HTMLResponse(
            content=f"""
            <div style="font-family: sans-serif; background: #09090b; color: #e4e4e7; height: 100vh; padding: 2rem;">
                <h2 style='color:#ef4444;'>Аккаунт не найден в базе лавки!</h2>
                <p>Twitch аккаунт <b>@{twitch_login}</b> еще не зарегистрирован в системе.</p>
                <p>Сначала зайди с этого аккаунта в Telegram-бота и привяжи Twitch, чтобы создать профиль, а затем повтори авторизацию здесь.</p>
                <br><a href='/rewards' style="color: #9146FF;">Вернуться назад</a>
            </div>
            """
        )
        
    if db_res.status_code not in [200, 204]:
        return HTMLResponse(content=f"<h2 style='color:red;'>Ошибка базы данных</h2><p>{db_res.text}</p>")
    
    return RedirectResponse(url="/settings")

@app.get("/api/v1/auth/logout")
async def logout():
    redirect = RedirectResponse(url="/")
    redirect.delete_cookie("admin_session")
    return redirect

# ==============================================================================
# 📊 3. АДМИН ПАНЕЛЬ И ДАШБОРДЫ
# ==============================================================================

@app.get("/api/v1/admin/stream_status")
async def get_stream_status(request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    """Читает статус стрима из таблицы settings"""
    if not request.cookies.get("admin_session"): 
        raise HTTPException(status_code=401)

    try:
        # Ищем оба ключа, которые могут указывать на онлайн
        res = await supabase.get("/rest/v1/settings", params={
            "key": "in.(twitch_status_883996654,twitch_status_755238101)"
        })
        
        is_online = False
        if res.status_code == 200:
            settings_data = res.json()
            for item in settings_data:
                val = item.get("value")
                # value в jsonb может распарситься как bool или как строка
                if val is True or val == "true" or val == True:
                    is_online = True
                    break
                    
        return {"is_online": is_online}
    except Exception as e:
        logging.error(f"Ошибка получения статуса стрима из БД: {e}")
        return {"is_online": False}

@app.get("/api/v1/admin/twitch_status")
async def get_twitch_status(request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    # 1. Проверяем сессию админа
    token = request.cookies.get("admin_session")
    if not token: 
        raise HTTPException(status_code=401)
    try:
        jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401)

    # Если разрешенных ID нет, возвращаем пустоту
    if not ALLOWED_IDS:
        return []

    # 2. Достаем стримерские аккаунты из базы
    allowed_ids_str = ",".join(ALLOWED_IDS)
    res = await supabase.get("/rest/v1/users", params={
        "twitch_id": f"in.({allowed_ids_str})",
        "select": "twitch_id,twitch_login,twitch_access_token"
    })
    
    if res.status_code != 200:
        return []
        
    users_data = res.json()
    
    # Запрашиваем настройки статусов перед проверкой токенов
    set_res = await supabase.get("/rest/v1/settings", params={"key": "in.(twitch_status_883996654,twitch_status_755238101)"})
    stream_statuses = {}
    if set_res.status_code == 200:
        for s in set_res.json():
            val = s.get("value")
            is_on = (val is True or val == "true" or val == True)
            if s["key"] == "twitch_status_755238101":
                stream_statuses["755238101"] = is_on
            elif s["key"] == "twitch_status_883996654":
                stream_statuses["883996654"] = is_on

    # 3. Функция валидации конкретного токена
    async def check_token(user):
        twitch_id = str(user.get('twitch_id'))
        login = user.get("twitch_login") or f"ID:{twitch_id}"
        access_token = user.get("twitch_access_token")
        is_stream_online = stream_statuses.get(twitch_id, False) # Забираем статус для конкретного канала
        
        if not access_token:
            return {"login": login, "is_valid": False, "is_online": is_stream_online}
            
        # Легкий запрос к Twitch для проверки жизни токена
        val_res = await http_client.get(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {access_token}"}
        )
        
        # Если статус 200 — токен жив, иначе 401 (протух)
        return {"login": login, "is_valid": val_res.status_code == 200, "is_online": is_stream_online}
        
    # 4. Проверяем все токены одновременно (параллельно), чтобы не тормозить загрузку панели
    tasks = [check_token(u) for u in users_data]
    status_list = await asyncio.gather(*tasks)
    
    return status_list

@app.get("/api/v1/admin/stats")
async def get_admin_dashboard_stats(request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    try:
        res = await supabase.post("/rest/v1/rpc/get_global_metrics", json={})
        return res.json() if res.status_code == 200 else {"error": "RPC Error"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/v1/admin/market_cache")
async def get_admin_market_cache_search(
    request: Request, search: str = "", cond: str = "all", rarity: str = "all",
    min_p: float = 0.0, max_p: float = 99999.0, sort: str = "desc", supabase: httpx.AsyncClient = Depends(get_supabase_client)
):
    try:
        jwt.decode(request.cookies.get("admin_session", ""), JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401)

    try:
        # 🔥 ДОБАВИЛИ "order": f"price_rub.{sort}"
        params = {"price_rub": f"gte.{min_p}", "and": f"(price_rub.lte.{max_p})", "order": f"price_rub.{sort}", "limit": "50"}
        if search: params["market_hash_name"] = f"ilike.*{search.strip()}*"
        if cond and cond != "all": params["market_hash_name"] = f"ilike.*({cond})*"
        if rarity and rarity != "all": params["rarity"] = f"eq.{rarity.lower()}"

        res = await supabase.get("/rest/v1/market_cache", params=params)
        return res.json() if res.status_code == 200 else []
    except Exception:
        return []

@app.get("/api/v1/admin/users")
async def get_admin_users(
    request: Request, search: str = "", hasTwitch: str = "all", trust: str = "all",
    sort_by: str = "total_message_count", supabase: httpx.AsyncClient = Depends(get_supabase_client)
):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    try:
        allowed_sorts = {
            "total_message_count": "total_message_count.desc", "weekly_message_count": "weekly_message_count.desc",
            "monthly_message_count": "monthly_message_count.desc", "telegram_total_message_count": "telegram_total_message_count.desc",
            "coins": "coins.desc", "tickets": "tickets.desc"
        }
        params = {
            "select": "telegram_id,full_name,photo_url,twitch_login,telegram_total_message_count,total_message_count,coins,tickets,trust_level",
            "order": allowed_sorts.get(sort_by, "total_message_count.desc"),
            "limit": "100"
        }
        if hasTwitch == "linked": params["twitch_login"] = "not.is.null"
        if trust != "all": params["trust_level"] = f"eq.{trust}"

        if search:
            s_clean = search.strip()
            if s_clean.isdigit(): params["telegram_id"] = f"eq.{s_clean}"
            else: params["or"] = f"(twitch_login.ilike.*{s_clean}*,full_name.ilike.*{s_clean}*)"

        res = await supabase.get("/rest/v1/users", params=params)
        return res.json() if res.status_code == 200 else []
    except Exception:
        return []

# ==============================================================================
# 🛠️ 4. РЕМОНТ И WEBHOOKS TWITCH
# ==============================================================================

@app.get("/api/v1/debug/fix_twitch_subs")
async def fix_twitch_subs(request: Request):
    """
    АДМИНСКАЯ ВЕРСИЯ: Только жесткое удаление подписок. 
    Админка не должна слушать Twitch, чтобы не было дублей!
    """
    try:
        jwt.decode(request.cookies.get("admin_session", ""), JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401)

    if not all([TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET]):
         return {"error": "Отсутствуют переменные окружения"}

    token_resp = await http_client.post(
        "https://id.twitch.tv/oauth2/token",
        data={"client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET, "grant_type": "client_credentials"}
    )
    if token_resp.status_code != 200: return {"error": "Twitch Auth Failed"}
    
    access_token = token_resp.json()["access_token"]
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}"}

    # 🔥 АГРЕССИВНОЕ УДАЛЕНИЕ: Сносим ВСЕ подписки админского приложения
    subs_resp = await http_client.get("https://api.twitch.tv/helix/eventsub/subscriptions", headers=headers)
    deleted_count = 0
    
    if subs_resp.status_code == 200:
        for sub in subs_resp.json().get("data", []):
            await http_client.delete(f"https://api.twitch.tv/helix/eventsub/subscriptions?id={sub['id']}", headers=headers)
            deleted_count += 1

    return {
        "message": "Успех! Админский бот очищен и больше не слушает Twitch (дубли устранены).", 
        "deleted_count": deleted_count
    }

# ==============================================================================
# 🔥 5. ВЫДЕЛЕННЫЙ БРОНЕЖИЛЕТ FOSSABOT (МАКСИМАЛЬНАЯ СКОРОСТЬ)
# ==============================================================================

async def process_bp_auto_quest(supabase: httpx.AsyncClient, keyword: str, tg_id: int = None, twitch_login: str = None):
    """
    Обработчик ручных/разовых триггеров. 
    Внедрен ЖЕСТКИЙ ЗАМОК (Linked List): проверяет статус предыдущей недели перед шагом вперед.
    """
    try:
        # 🔥 ИСПРАВЛЕНИЕ: Добавили /rest/v1/ ко всем запросам к Supabase!
        
        if not tg_id and twitch_login:
            u_res = await supabase.get("/rest/v1/users", params={"twitch_login": f"ilike.{twitch_login}", "select": "telegram_id"})
            if u_res.status_code == 200 and u_res.json():
                tg_id = u_res.json()[0]["telegram_id"]
        
        if not tg_id: return 

        cp_res = await supabase.get("/rest/v1/pages_content", params={"page_name": "eq.checkpoint", "select": "content"})
        if cp_res.status_code != 200 or not cp_res.json(): return
        
        config = cp_res.json()[0].get("content", {})
        if not config.get("is_active") or not config.get("start_date"): return
        
        msk_tz = timezone(timedelta(hours=3))
        start_date = datetime.fromisoformat(config["start_date"].replace('Z', '+00:00')).astimezone(msk_tz)
        now = datetime.now(msk_tz)
        
        if now < start_date: return
        
        days_passed = (now.date() - start_date.date()).days
        current_week = (days_passed // 7) + 1
        
        active_quests = [q for q in config.get("quests_config", []) if int(q.get("week", 1)) <= current_week]
        if not active_quests: return
        
        quest_ids = [str(q["quest_id"]) for q in active_quests]
        
        quests_res = await supabase.get("/rest/v1/quests", params={"id": f"in.({','.join(quest_ids)})", "select": "id,title,quest_type"})
        if quests_res.status_code != 200: return
        
        quests_db_data = quests_res.json()
        target_quest_type = None
        
        chain_quest_ids = []
        for q_db in quests_db_data:
            if keyword.lower() in q_db["title"].lower():
                chain_quest_ids.append(q_db["id"])
                
        if not chain_quest_ids: return
        
        chain_ids_str = ",".join(map(str, chain_quest_ids))
        
        target_configs = sorted([q for q in active_quests if int(q["quest_id"]) in chain_quest_ids], key=lambda x: int(x.get("week", 1)))
        
        prog_res = await supabase.get("/rest/v1/user_bp_quests", params={
            "user_id": f"eq.{tg_id}",
            "quest_id": f"in.({chain_ids_str})"
        })
        
        user_progress = {}
        if prog_res.status_code == 200:
            for q in prog_res.json():
                w = q.get("week")
                q_id = q.get("quest_id")
                if q_id is not None:
                    user_progress[(int(q_id), int(w) if w is not None else 1)] = q
        
        week_to_update = None
        target_amount = 1
        current_db_record = None
        target_quest_id_for_week = None
        previous_cleared = True
        
        for cfg in target_configs:
            w = int(cfg.get("week", 1))
            q_id = int(cfg.get("quest_id"))
            
            prog = user_progress.get((q_id, w))
            
            if not previous_cleared:
                break
                
            if prog:
                if prog.get("is_completed") and not prog.get("is_claimed"):
                    break
                elif not prog.get("is_completed"):
                    week_to_update = w
                    target_amount = int(cfg.get("target_amount", cfg.get("target", 1)))
                    current_db_record = prog
                    target_quest_id_for_week = q_id
                    break
                else:
                    previous_cleared = True
            else:
                week_to_update = w
                target_amount = int(cfg.get("target_amount", cfg.get("target", 1)))
                current_db_record = None
                target_quest_id_for_week = q_id
                break
                
        if not week_to_update or not target_quest_id_for_week: return 
        
        if current_db_record:
            new_amount = current_db_record["current_amount"] + 1
            is_completed = new_amount >= target_amount
            
            record_id = current_db_record.get('id')
            if record_id:
                patch_params = {"id": f"eq.{record_id}"}
            else:
                patch_params = {
                    "user_id": f"eq.{tg_id}",
                    "quest_id": f"eq.{target_quest_id_for_week}",
                    "week": f"eq.{week_to_update}"
                }
                
            await supabase.patch("/rest/v1/user_bp_quests", params=patch_params, json={
                "current_amount": new_amount,
                "is_completed": is_completed
            })
        else:
            is_completed = 1 >= target_amount
            await supabase.post("/rest/v1/user_bp_quests", json={
                "user_id": tg_id,
                "quest_id": int(target_quest_id_for_week),
                "week": week_to_update,
                "current_amount": 1,
                "target_amount": target_amount,
                "is_completed": is_completed,
                "is_claimed": False
            })
            
    except Exception as e:
        logging.error(f"Ошибка в авто-квесте БП ({keyword}): {e}", exc_info=True)

async def process_round_end(supabase: httpx.AsyncClient, target_filter: str, current_word: str):
    try:
        # Мгновенно отправляем сигнал в OBS (открыть слово и включить паузу)
        await broadcast_guess_update(supabase, "force-update", {
            "current_word": target_filter,
            "revealed_indices": list(range(len(target_filter))),
            "is_cooldown": True,
            "action": "cooldown",  # Сигнал для таймера OBS
            "delay": 20
        })
        
        # Мы УДАЛИЛИ отсюда генерацию слова и ожидание 18.5 секунд. 
        # Эту работу теперь делает OBS и эндпоинт /obs_next_round.
        # Функция завершается за миллисекунды.

    except Exception as e:
        print(f"DEBUG BACKGROUND ERROR: {e}")


guess_cache = {
    "word": None, "is_active": False, "updated_at": 0, "raw_word": "",
    "cooldown_until": 0, "buffer_end": 0, "round_winners": []
}


@app.get("/api/v1/twitch/fossabot_guess", response_class=PlainTextResponse)
async def handle_fossabot_guess(request: Request, background_tasks: BackgroundTasks):
    token = request.headers.get("x-fossabot-customapitoken") or request.query_params.get("token")
    if not token: return ""

    try:
        global guess_cache
        now = time.time()

        if now > guess_cache.get("buffer_end", 0) and now < guess_cache.get("cooldown_until", 0):
            return ""

        if now - guess_cache["updated_at"] > 10:
            guess_cache["updated_at"] = now
            try:
                state_res = await supabase_client.get("/rest/v1/guess_state", params={"id": "eq.1"})
                if state_res.status_code == 200 and state_res.json():
                    state = state_res.json()[0]
                    guess_cache["raw_word"] = state.get("current_word", "")
                    guess_cache["word"] = guess_cache["raw_word"].upper()
                    guess_cache["is_active"] = state.get("is_active", False)
            except Exception:
                guess_cache["updated_at"] = 0

        if not guess_cache["is_active"] or not guess_cache["word"]: return ""

        fb_res = await http_client.get(f"https://api.fossabot.com/v2/customapi/context/{token}", timeout=3.0)
        if fb_res.status_code != 200: return ""
        
        msg_data = fb_res.json().get("message")
        if not msg_data: return ""

        twitch_login = msg_data["user"]["login"].lower()
        twitch_display = msg_data["user"]["display_name"]
        guess_word = msg_data["content"].strip().upper()

        if guess_word != guess_cache["word"]: return ""

        is_first_blood = False
        if now > guess_cache.get("cooldown_until", 0):
            guess_cache["buffer_end"] = now + 1.5        
            guess_cache["cooldown_until"] = now + 20    
            guess_cache["round_winners"] = []            
            is_first_blood = True
            
        if twitch_display not in guess_cache["round_winners"]:
            guess_cache["round_winners"].append(twitch_display)
            background_tasks.add_task(supabase_client.post, "/rest/v1/rpc/increment_guess_score", json={"p_twitch_login": twitch_login})
            
            # 🔥 ВЕРНУЛИ ВЫЗОВ АВТОКВЕСТА ДЛЯ БАТЛПАССА
            try:
                background_tasks.add_task(process_bp_auto_quest, supabase_client, "отгадай", None, twitch_login)
            except Exception as e:
                print(f"DEBUG CRITICAL TASK ERROR: {e}")

        if is_first_blood:
            target_filter = guess_cache["raw_word"]
            
            # 🔥 ВЕРНУЛИ СИГНАЛ В OBS ДЛЯ ЗАВЕРШЕНИЯ РАУНДА
            background_tasks.add_task(process_round_end, supabase_client, target_filter, guess_cache["raw_word"])

            background_tasks.add_task(
                supabase_client.patch, "/rest/v1/guess_state", 
                params={"id": "eq.1"}, json={"revealed_indices": list(range(len(guess_cache["raw_word"])))}
            )
            winners_str = ", @".join(guess_cache["round_winners"])
            return f"🎉 Слово «{guess_cache['word']}» угадано! Очки забирают: @{winners_str}. След. слово через 20с."
        
        return ""
    except Exception:
        return ""

# ==============================================================================
# 🎁 6. НАГРАДЫ TWITCH И КОРОБКИ
# ==============================================================================

class TwitchRaffleCreateRequest(BaseModel):
    title: str
    cost: int
    winners_count: int = 1
    broadcaster_id: str
    is_for_newbies: bool = False
    min_lifetime_msgs: int = 0
    image_url: Optional[str] = None
    steps: Optional[list] = [] 
    prize_price: Optional[float] = 0.0      # 🔥 ДОБАВЛЕНО
    skin_quality: Optional[str] = ""        # 🔥 ДОБАВЛЕНО
    rarity_color: Optional[str] = "#9146FF" # 🔥 ДОБАВЛЕНО

@app.post("/api/v1/admin/raffles/create_twitch")
async def create_twitch_raffle(req: TwitchRaffleCreateRequest, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    
    token_res = await supabase.get("/rest/v1/users", params={"twitch_id": f"eq.{req.broadcaster_id}", "select": "twitch_access_token"})
    if token_res.status_code != 200 or not token_res.json():
        raise HTTPException(status_code=401, detail="Токен стримера не найден")
        
    broadcaster_token = token_res.json()[0]["twitch_access_token"]

    reward_title = f"Розыгрыш: {req.title}"
    if len(reward_title) > 45:
        reward_title = reward_title[:44] + "…"

    twitch_url = f"https://api.twitch.tv/helix/channel_points/custom_rewards?broadcaster_id={req.broadcaster_id}"
    headers = {"Authorization": f"Bearer {broadcaster_token}", "Client-Id": TWITCH_CLIENT_ID, "Content-Type": "application/json"}
    
    tw_res = await http_client.post(twitch_url, headers=headers, json={
        "title": reward_title,
        "cost": req.cost,
        "is_user_input_required": True,
        "background_color": "#9146FF"
    })
    
    if tw_res.status_code == 401:
        raise HTTPException(status_code=401, detail="Токен истек или недействителен")
        
    twitch_reward_id = None
    if tw_res.status_code == 400 and "DUPLICATE_REWARD" in tw_res.text:
        # 🔥 Перехват: Ищем старую награду на Твиче и берем её ID
        get_url = f"https://api.twitch.tv/helix/channel_points/custom_rewards?broadcaster_id={req.broadcaster_id}&only_manageable_rewards=true"
        get_res = await http_client.get(get_url, headers=headers)
        if get_res.status_code == 200:
            for r in get_res.json().get("data", []):
                if r.get("title") == reward_title:
                    twitch_reward_id = r.get("id")
                    break
        
        if twitch_reward_id:
            # Обновляем старую награду: ставим новую цену и включаем
            patch_url = f"https://api.twitch.tv/helix/channel_points/custom_rewards?broadcaster_id={req.broadcaster_id}&id={twitch_reward_id}"
            await http_client.patch(patch_url, headers=headers, json={
                "cost": req.cost, "is_user_input_required": True, "background_color": "#9146FF", "is_enabled": True
            })
        else:
            raise HTTPException(status_code=400, detail=f"Награда '{reward_title}' уже существует на Twitch, но бот не имеет прав на её изменение. Пожалуйста, удали её вручную в панели управления Twitch.")
            
    elif tw_res.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Ошибка Twitch: {tw_res.text}")
    else:
        twitch_reward_id = tw_res.json()["data"][0]["id"]

    # 🔥 1. СОХРАНЯЕМ В ТАБЛИЦУ НАГРАД (С защитой от дубликатов)
    rw_payload = {
        "title": reward_title,
        "reward_type": "raffle", 
        "broadcaster_id": req.broadcaster_id,
        "twitch_reward_id": twitch_reward_id,
        "cost": req.cost,
        "is_active": True,
        "steam_item_name": req.title,
        "show_user_input": True,
        "platform": "twitch",
        "condition_type": "none"
    }
    
    # Ищем, была ли уже такая награда в БД
    check_db = await supabase.get("/rest/v1/twitch_rewards", params={"title": f"eq.{reward_title}", "select": "id"})
    
    if check_db.status_code == 200 and check_db.json():
        # Если есть — просто обновляем её новым ID от Twitch (PATCH)
        existing_id = check_db.json()[0]["id"]
        db_rw = await supabase.patch("/rest/v1/twitch_rewards", params={"id": f"eq.{existing_id}"}, json=rw_payload, headers={"Prefer": "return=representation"})
    else:
        # Если нет — создаем новую (POST)
        db_rw = await supabase.post("/rest/v1/twitch_rewards", json=rw_payload, headers={"Prefer": "return=representation"})
        
    if db_rw.status_code not in [200, 201]:
        raise HTTPException(status_code=400, detail=f"Ошибка БД (twitch_rewards): {db_rw.text}")
        
    internal_reward_id = db_rw.json()[0]["id"]

    # 🔥 2. СОХРАНЯЕМ САМ РОЗЫГРЫШ (С привязкой ID)
    raf_payload = {
        "title": req.title,
        "type": "twitch_fossabot", 
        "status": "active",
        "start_time": datetime.now(timezone.utc).isoformat(),
        "settings": {
            "required_twitch_reward_id": internal_reward_id,
            "real_twitch_reward_id": twitch_reward_id,
            "twitch_reward_title": reward_title,
            "winners_count": req.winners_count,
            "prize_image": req.image_url,
            "is_for_newbies": req.is_for_newbies,
            "min_lifetime_msgs": req.min_lifetime_msgs,
            "steps": req.steps,
            "prize_name": req.title,          # 🔥 ТЕПЕРЬ СОХРАНЯЕТ ИМЯ
            "prize_price": req.prize_price,   # 🔥 ТЕПЕРЬ СОХРАНЯЕТ ЦЕНУ
            "skin_quality": req.skin_quality, # 🔥 ТЕПЕРЬ СОХРАНЯЕТ КАЧЕСТВО
            "rarity_color": req.rarity_color  # 🔥 ТЕПЕРЬ СОХРАНЯЕТ ЦВЕТ
        }
    }
    
    db_raf = await supabase.post("/rest/v1/raffles", json=raf_payload)
    if db_raf.status_code not in [200, 201, 204]:
        raise HTTPException(status_code=400, detail="Ошибка БД при сохранении розыгрыша")
        
    return {"status": "success", "message": "Twitch розыгрыш запущен!"}

@app.get("/api/v1/twitch/fossabot_raffle", response_class=PlainTextResponse)
async def handle_fossabot_raffle(request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    print("===== [FOSSABOT RAFFLE] ЗАПРОС ПОЛУЧЕН =====")
    try:
        # Забираем токен
        token = request.headers.get("x-fossabot-customapitoken") or request.query_params.get("token")
        print(f"[FOSSABOT RAFFLE] Токен: {token}")
        
        if not token: 
            return "❌ Ошибка: Токен Фоссабота не передан."

        # 1. Запрашиваем контекст из Fossabot
        fb_url = f"https://api.fossabot.com/v2/customapi/context/{token}"
        print(f"[FOSSABOT RAFFLE] Идем в FossaBot API: {fb_url}")
        
        fb_res = await http_client.get(fb_url, timeout=5.0)
        print(f"[FOSSABOT RAFFLE] Статус от FossaBot: {fb_res.status_code}")
        
        if fb_res.status_code != 200: 
            return f"❌ Ошибка связи с сервером Fossabot. Код: {fb_res.status_code}"
            
        msg_data = fb_res.json().get("message")
        if not msg_data: 
            print("[FOSSABOT RAFFLE] Пустой message в ответе FossaBot!")
            return "❌ Ошибка: Пустой ответ от Fossabot API."

        twitch_login = msg_data["user"]["login"].lower()
        twitch_display = msg_data["user"]["display_name"]
        print(f"[FOSSABOT RAFFLE] Зритель: {twitch_display} ({twitch_login})")

        # 2. Достаем активный Twitch-розыгрыш
        print("[FOSSABOT RAFFLE] Ищем розыгрыш в БД...")
        res = await supabase.get("/rest/v1/raffles", params={
            "status": "eq.active",
            "type": "eq.twitch_fossabot",
            "select": "id,title,settings",
            "limit": "1"
        })
        print(f"[FOSSABOT RAFFLE] Ответ БД (розыгрыш): {res.status_code}")
        
        if res.status_code != 200 or not res.json():
            print("[FOSSABOT RAFFLE] Розыгрышей за баллы нет.")
            return f"@{twitch_display}, сейчас нет активных розыгрышей за баллы! 🐸"
            
        raffle = res.json()[0]
        settings = raffle.get("settings", {})
        reward_title = settings.get("twitch_reward_title", "Участие в розыгрыше")
        is_for_newbies = settings.get("is_for_newbies", False)
        min_msgs = settings.get("min_lifetime_msgs", 0)
        
        print(f"[FOSSABOT RAFFLE] Условия: новички={is_for_newbies}, мин.сообщений={min_msgs}")

        # 3. ИЩЕМ ЮЗЕРА В БД (🔥 ТЕПЕРЬ ИЩЕМ ЕЩЕ И ССЫЛКУ)
        print("[FOSSABOT RAFFLE] Ищем юзера в БД...")
        user_res = await supabase.get("/rest/v1/users", params={
            "twitch_login": f"eq.{twitch_login}",
            "select": "telegram_id, total_message_count, trade_link"
        })
        
        user_data = user_res.json() if user_res.status_code == 200 else []
        is_linked = len(user_data) > 0 and user_data[0].get("telegram_id") is not None
        db_msgs = user_data[0].get("total_message_count", 0) if user_data else 0
        
        # Проверяем, есть ли у него привязанная трейд-ссылка
        has_trade_link = user_data[0].get("trade_link") if is_linked else None

        print(f"[FOSSABOT RAFFLE] Привязан: {is_linked}, Ссылка есть: {bool(has_trade_link)}")

        # 🛑 ЛОГИКА ФИЛЬТРАЦИИ:
        if is_for_newbies and is_linked:
            print("[FOSSABOT RAFFLE] Отказ: юзер уже есть в базе ТГ.")
            return f"@{twitch_display}, у тебя уже привязан ТГ-бот! Участвуй в основных розыгрышах там, оставь этот новичкам! ❌"
            
        if min_msgs > 0 and db_msgs < min_msgs:
            print("[FOSSABOT RAFFLE] Отказ: мало сообщений.")
            return f"@{twitch_display}, у тебя недостаточно сообщений в чате (нужно {min_msgs}, а у тебя {db_msgs}). Общайся больше! ❌"

        # 4. Юзер прошел проверки! Отдаем умный ответ.
        print("[FOSSABOT RAFFLE] УСПЕХ! Выдаем инструкцию.")
        if has_trade_link:
            return (
                f"@{twitch_display}, ты в базе! ❗️ДЛЯ УЧАСТИЯ: Купи награду «{reward_title}» "
                f"и просто отправь туда плюсик «+», трейд-ссылку мы возьмем из твоего профиля! 🎁"
            )
        else:
            return (
                f"@{twitch_display}, ты прошел проверку! ❗️ДЛЯ УЧАСТИЯ: Купи награду «{reward_title}» "
                f"и ОБЯЗАТЕЛЬНО вставь туда свою трейд-ссылку! 🎁"
            )

    except Exception as e:
        print(f"!!!!! [FOSSABOT RAFFLE] КРИТИЧЕСКАЯ ОШИБКА !!!!!\n{e}")
        import traceback
        traceback.print_exc()
        return "❌ Ошибка сервера Vercel. Посмотри логи."

import random
from fastapi import BackgroundTasks, Request, Depends, HTTPException

# 🔥 НОВАЯ ИЗОЛИРОВАННАЯ ФУНКЦИЯ ДЛЯ СТУПЕНЕЙ
async def check_and_upgrade_raffle_prize(supabase: httpx.AsyncClient, raffle_id: int, current_participants_count: int, settings: dict):
    """
    Проверяет, достигнута ли новая ступень участников.
    Если да — перезаписывает prize_name и prize_price в settings.
    Для старых розыгрышей (где нет steps) ничего не делает.
    """
    steps = settings.get("steps", [])
    if not steps:
        return settings # Ступеней нет, выходим

    # Находим все ступени, до которых мы уже дошли
    valid_steps = [s for s in steps if current_participants_count >= int(s.get("participants_required", 0))]
    
    if not valid_steps:
        return settings # Еще не дошли даже до первой ступени
        
    # Берем самую высокую достигнутую ступень
    best_step = max(valid_steps, key=lambda x: int(x.get("participants_required", 0)))
    
    # Если текущий приз уже от этой ступени, лишний раз БД не дергаем
    current_step_p = settings.get("current_step_participants", -1)
    if current_step_p == best_step["participants_required"]:
        return settings 

    # 🔥 ОБНОВЛЯЕМ ПРИЗ
    settings["prize_name"] = best_step["prize_name"]
    settings["prize_price"] = best_step.get("prize_price", settings.get("prize_price", 0))
    settings["skin_quality"] = best_step.get("skin_quality", "") # <-- Эта
    settings["rarity_color"] = best_step.get("rarity_color", "#9146FF") # <-- И эта
    settings["current_step_participants"] = best_step["participants_required"]
    
    # Тихо обновляем БД. Основной ТГ-бот автоматически увидит новое название!
    await supabase.patch("/rest/v1/raffles", params={"id": f"eq.{raffle_id}"}, json={"settings": settings})
    logging.info(f"[RAFFLE UPGRADE] Розыгрыш #{raffle_id} улучшен до {settings['prize_name']}!")
    
    return settings

@app.post("/api/v1/admin/raffles/{id}/complete")
async def complete_raffle(
    id: int, 
    request: Request, 
    background_tasks: BackgroundTasks,
    supabase: httpx.AsyncClient = Depends(get_supabase_client)
):
    # 1. Проверка админ-сессии
    if not request.cookies.get("admin_session"): 
        raise HTTPException(status_code=401)

    # 2. Получаем данные розыгрыша
    raf_res = await supabase.get("/rest/v1/raffles", params={"id": f"eq.{id}", "select": "*"})
    if raf_res.status_code != 200 or not raf_res.json():
        raise HTTPException(status_code=404, detail="Розыгрыш не найден")
        
    raffle = raf_res.json()[0]
    
    if raffle.get("status") == "completed":
        return {"status": "error", "message": "Розыгрыш уже завершен"}

    # 3. Получаем всех участников розыгрыша (с trade_link)
    part_res = await supabase.get("/rest/v1/raffle_participants", params={
        "raffle_id": f"eq.{id}",
        "select": "*,users(full_name,twitch_login,trade_link)"
    })
    
    participants = part_res.json() if part_res.status_code == 200 else []
    if not participants:
        return {"status": "error", "message": "Нет участников для проведения розыгрыша"}

    # Финальный перерасчет ступени перед выдачей!
    real_participants_count = len(participants)
    raffle_settings = raffle.get("settings", {})
    raffle_settings = await check_and_upgrade_raffle_prize(supabase, id, real_participants_count, raffle_settings)

    # 4. Выбор победителя
    tickets = []
    for p in participants:
        score = p.get("score", 1)
        tickets.extend([p] * score)
        
    winner = random.choice(tickets)
    tg_id = winner.get("user_id")

    # 5. Обновляем статус розыгрыша на completed и записываем победителя
    await supabase.patch(
        "/rest/v1/raffles", 
        params={"id": f"eq.{id}"}, 
        json={"status": "completed", "winner_id": tg_id}
    )

    # 🔥 НОВОЕ: АВТО-УДАЛЕНИЕ НАГРАДЫ С TWITCH И ИЗ БАЗЫ 🔥
    internal_reward_id = raffle_settings.get("required_twitch_reward_id")
    if internal_reward_id:
        try:
            # Ищем награду в нашей базе
            rew_res = await supabase.get("/rest/v1/twitch_rewards", params={"id": f"eq.{internal_reward_id}", "select": "broadcaster_id, twitch_reward_id"})
            if rew_res.status_code == 200 and rew_res.json():
                b_id = rew_res.json()[0].get("broadcaster_id")
                t_id = rew_res.json()[0].get("twitch_reward_id")
                
                # Идем на Twitch удалять награду
                if b_id and t_id:
                    tok_res = await supabase.get("/rest/v1/users", params={"twitch_id": f"eq.{b_id}", "select": "twitch_access_token"})
                    if tok_res.status_code == 200 and tok_res.json():
                        b_token = tok_res.json()[0].get("twitch_access_token")
                        if b_token:
                            twitch_url = f"https://api.twitch.tv/helix/channel_points/custom_rewards?broadcaster_id={b_id}&id={t_id}"
                            # Отправляем DELETE запрос в Twitch
                            await http_client.delete(twitch_url, headers={"Authorization": f"Bearer {b_token}", "Client-Id": TWITCH_CLIENT_ID})
                            logging.info(f"[RAFFLE CLEANUP] Награда {t_id} удалена с Twitch канала {b_id}")
                            
            # Удаляем саму награду из нашей БД, чтобы не висела в списке наград
            await supabase.delete("/rest/v1/twitch_rewards", params={"id": f"eq.{internal_reward_id}"})
            logging.info(f"[RAFFLE CLEANUP] Награда удалена из базы twitch_rewards")
        except Exception as e:
            logging.error(f"[RAFFLE CLEANUP] Ошибка при удалении награды: {e}")

    # 6. Начисление приза
    base_prize_name = raffle_settings.get("prize_name", raffle.get("title", "Секретный приз"))
    prize_price = raffle_settings.get("prize_price", 0.0)
    skin_quality = raffle_settings.get("skin_quality", "")

    # СОБИРАЕМ ТОЧНОЕ ИМЯ ДЛЯ МАРКЕТА
    full_prize_name = base_prize_name.strip()
    quality_map = {
        "FN": "Factory New", "MW": "Minimal Wear", "FT": "Field-Tested", 
        "WW": "Well-Worn", "BS": "Battle-Scarred"
    }
    
    if skin_quality and skin_quality in quality_map:
        eng_quality = quality_map[skin_quality]
        if not re.search(r'\(.*?\)', full_prize_name): 
            full_prize_name = f"{full_prize_name} ({eng_quality})"
            
    if tg_id:
        # Забираем трейд-ссылку из данных победителя
        user_data_db = winner.get("users") or {}
        trade_link = user_data_db.get("trade_link")

        # Ищем ID предмета и ЕГО ЦЕНУ в каталоге
        item_res = await supabase.get("/rest/v1/cs_items", params={
            "market_hash_name": f"eq.{full_prize_name}",  
            "select": "id, price_rub", 
            "limit": 1
        })
        
        item_id = None
        if item_res.status_code == 200 and item_res.json():
            item_data = item_res.json()[0]
            item_id = item_data["id"]
            
            if float(prize_price) <= 0:
                prize_price = float(item_data.get("price_rub", 0.0))
                logging.info(f"Подтянули цену со склада для {full_prize_name}: {prize_price} руб.")
                
        # БРОНЕЖИЛЕТ: Если цена ВСЁ РАВНО 0, тянем её напрямую из market_cache
        if float(prize_price) <= 0:
            cache_res = await supabase.get("/rest/v1/market_cache", params={
                "market_hash_name": f"eq.{full_prize_name}", "select": "price_rub", "limit": 1
            })
            if cache_res.status_code == 200 and cache_res.json():
                prize_price = float(cache_res.json()[0].get("price_rub", 0.0))
                logging.info(f"Подтянули резервную цену из market_cache для {full_prize_name}: {prize_price} руб.")

        initial_status = "processing" if trade_link else "available"

        history_res = await supabase.post("/rest/v1/cs_history", json={
            "user_id": tg_id,
            "item_id": item_id,
            "status": initial_status,
            "case_name": "Победа в розыгрыше",
            "details": f"Выигрыш: {full_prize_name}", 
            "source": "raffle",
            "is_swapped": False
        }, headers={"Prefer": "return=representation"})
        
        # 7. Запускаем покупку на маркете в фоне
        if trade_link and history_res.status_code in [200, 201] and history_res.json():
            history_id = history_res.json()[0]["id"]
            
            background_tasks.add_task(
                direct_market_buy_for_raffle,
                client=supabase,
                trade_link=trade_link,
                prize_name=full_prize_name, 
                prize_price=float(prize_price),
                history_id=history_id
            )

    return {
        "status": "success", 
        "winner": winner.get("users", {}).get("full_name", str(tg_id))
    }
    
# 🔥 ДОБАВЛЯЕМ ЭТУ ФУНКЦИЮ СРАЗУ ПОСЛЕ complete_raffle
async def direct_market_buy_for_raffle(client, trade_link: str, prize_name: str, prize_price: float, history_id: int):
    """
    Прямая закупка скина на CSGO Market для победителя розыгрыша.
    """
    logging.info(f"[RAFFLE] Покупаем {prize_name} для history_id #{history_id}")
    
    TM_API_KEY = os.getenv("CSGO_MARKET_API_KEY") 
    if not TM_API_KEY:
        logging.error("Нет ключа CSGO_MARKET_API_KEY!")
        await client.patch("/rest/v1/cs_history", params={"id": f"eq.{history_id}"}, json={"status": "Ошибка: Нет API ключа"})
        return

    market = MarketCSGO(api_key=TM_API_KEY)
    unique_market_id = f"raf_{history_id}_{int(time.time())}"
    
    # Пытаемся купить
    market_res = await market.buy_for_user(
        hash_name=prize_name,
        max_price_rub=prize_price,
        trade_link=trade_link,
        custom_id=unique_market_id
    )
    
    if market_res.get("success"):
        logging.info(f"[RAFFLE] ✅ Успешно куплено на Маркете!")
        await client.patch("/rest/v1/cs_history", params={"id": f"eq.{history_id}"}, json={
            "status": "market_pending", 
            "tradeofferid": unique_market_id
        })
    else:
        err_msg = market_res.get("error", "Ошибка Маркета")
        logging.error(f"[RAFFLE] ❌ Ошибка Маркета: {err_msg}")
        # При ошибке откатываем статус на "available", чтобы юзер забрал позже
        await client.patch("/rest/v1/cs_history", params={"id": f"eq.{history_id}"}, json={
            "status": "available",
            "details": f"Сбой автовывода: {err_msg}"
        })
        
class RewardCreateRequest(BaseModel):
    title: str
    reward_type: str
    broadcaster_id: str
    cost: int 
    linked_box_id: Optional[int] = None
    steam_item_name: Optional[str] = ""
    auto_steam: Optional[bool] = False
    reward_amount: Optional[int] = 10
    target_value: Optional[int] = 0
    gate_period: Optional[str] = "session"
    notify_admin: Optional[bool] = True
    show_user_input: Optional[bool] = True
    target_audience: Optional[str] = "all"

@app.post("/api/v1/admin/rewards/create")
async def create_admin_twitch_reward(req: RewardCreateRequest, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): 
        raise HTTPException(status_code=401)
    
    # 1. Достаем токен выбранного стримера
    token_res = await supabase.get("/rest/v1/users", params={"twitch_id": f"eq.{req.broadcaster_id}", "select": "twitch_access_token"})
    token_data = token_res.json()
    if not token_data or not token_data[0].get("twitch_access_token"):
        raise HTTPException(status_code=400, detail="Токен стримера не найден. Нажми 'Обновить токен стримера' в шапке.")
    
    broadcaster_token = token_data[0]["twitch_access_token"]
    twitch_reward_id = None
    
    # 2. Создаем награду на Twitch с указанной ценой
    try:
        twitch_url = f"https://api.twitch.tv/helix/channel_points/custom_rewards?broadcaster_id={req.broadcaster_id}"
        headers = {
            "Authorization": f"Bearer {broadcaster_token}", 
            "Client-Id": TWITCH_CLIENT_ID, 
            "Content-Type": "application/json"
        }
        
        tw_res = await http_client.post(twitch_url, headers=headers, json={
            "title": req.title,
            "cost": req.cost, # 🔥 ПЕРЕДАЕМ ЦЕНУ НА ТВИЧ
            "is_user_input_required": req.show_user_input
        })
        
        if tw_res.status_code == 200:
            twitch_reward_id = tw_res.json()["data"][0]["id"]
        elif tw_res.status_code == 401:
            raise HTTPException(status_code=401, detail="Токен Twitch истек. Нажми 'Обновить токен стримера' в шапке.")
        elif tw_res.status_code != 400: 
            # 400 - это скорее всего дубликат (награда уже существует), прощаем
            logging.warning(f"Ошибка Твича: {tw_res.text}")
            
    except HTTPException:
        raise # Прокидываем 401 или 400 дальше на фронт
    except Exception as e:
        logging.error(f"Сбой Твича: {e}")

    # 3. Формируем данные для нашей БД
    payload = req.dict(exclude_unset=True)
    gate_period = payload.pop("gate_period", "session")
    
    condition_map = {
        "session": "twitch_messages_session",
        "week": "twitch_messages_week",
        "month": "twitch_messages_month"
    }
    
    payload["promocode_amount"] = req.reward_amount
    payload["condition_type"] = condition_map.get(gate_period, "none") if req.target_value > 0 else "none"
    payload["is_active"] = True
    
    if twitch_reward_id: 
        payload["twitch_reward_id"] = twitch_reward_id 
        
    # 4. Сохраняем в БД
    res = await supabase.post("/rest/v1/twitch_rewards", json=payload)
    if res.status_code in [200, 201, 204]: 
        return {"status": "success"}
        
    raise HTTPException(status_code=400, detail=res.text)
    
@app.delete("/api/v1/admin/rewards/{reward_id}/delete")
async def delete_admin_twitch_reward(reward_id: int, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    
    # 1. Получаем информацию о награде
    reward_resp = await supabase.get("/rest/v1/twitch_rewards", params={"id": f"eq.{reward_id}", "select": "twitch_reward_id, broadcaster_id"})
    reward_data = reward_resp.json()
    
    if not reward_data:
        raise HTTPException(status_code=404, detail="Награда не найдена в БД")
        
    reward = reward_data[0]
    twitch_reward_id = reward.get("twitch_reward_id")
    broadcaster_id = reward.get("broadcaster_id")

    # 2. Если есть ID награды Твича и стримера, пытаемся удалить её с самого Twitch
    if twitch_reward_id and broadcaster_id:
        # Достаем токен стримера
        token_resp = await supabase.get("/rest/v1/users", params={"twitch_id": f"eq.{broadcaster_id}", "select": "twitch_access_token"})
        token_data = token_resp.json()
        
        if token_data and token_data[0].get("twitch_access_token"):
            broadcaster_token = token_data[0]["twitch_access_token"]
            
            twitch_url = f"https://api.twitch.tv/helix/channel_points/custom_rewards?broadcaster_id={broadcaster_id}&id={twitch_reward_id}"
            headers = {
                "Authorization": f"Bearer {broadcaster_token}",
                "Client-Id": TWITCH_CLIENT_ID
            }
            
            # Запрос к Twitch на удаление
            tw_res = await http_client.delete(twitch_url, headers=headers)
            
            # 404 значит, что награды уже нет на Твиче, 204 - успешно удалено
            if tw_res.status_code not in [200, 204, 404]:
                logging.warning(f"Twitch вернул ошибку при удалении награды {twitch_reward_id}: {tw_res.text}")

    # 3. Удаляем награду из нашей базы данных
    db_res = await supabase.delete("/rest/v1/twitch_rewards", params={"id": f"eq.{reward_id}"})
    if db_res.status_code not in [200, 204]:
        raise HTTPException(status_code=400, detail="Ошибка удаления из базы данных")
        
    return {"status": "success"}



@app.get("/api/v1/admin/rewards")
async def get_admin_rewards_panel(request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    try: 
        jwt.decode(request.cookies.get("admin_session", ""), JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError: 
        raise HTTPException(status_code=401)

    try:
        # 1. Тянем статусы из БД
        set_res = await supabase.get("/rest/v1/settings", params={"key": "in.(twitch_status_883996654,twitch_status_755238101)"})
        stream_statuses = {}
        if set_res.status_code == 200:
            for s in set_res.json():
                val = s.get("value")
                is_on = (val is True or val == "true" or val == True)
                if s["key"] == "twitch_status_755238101":
                    stream_statuses["755238101"] = is_on
                elif s["key"] == "twitch_status_883996654":
                    stream_statuses["883996654"] = is_on  # ID для hatelove_ttv

        channels_metadata = []
        token_resp = await http_client.post(
            "https://id.twitch.tv/oauth2/token",
            data={"client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET, "grant_type": "client_credentials"}
        )
        if token_resp.status_code == 200:
            app_token = token_resp.json()["access_token"]
            tw_res = await http_client.get(
                "https://api.twitch.tv/helix/users",
                headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {app_token}"},
                params=[("id", b_id) for b_id in ALLOWED_IDS]
            )
            if tw_res.status_code == 200:
                channels_metadata = [
                    {
                        "id": u["id"], 
                        "login": u["login"], 
                        "display_name": u["display_name"], 
                        "profile_image": u["profile_image_url"],
                        "is_online": stream_statuses.get(u["id"], False) # Прокидываем статус!
                    }
                    for u in tw_res.json().get("data", [])
                ]

        if not channels_metadata:
            channels_metadata = [{"id": b, "login": f"Channel_{b}", "display_name": f"ID: {b}", "profile_image": "", "is_online": stream_statuses.get(b, False)} for b in ALLOWED_IDS]

        # ЭТОТ БЛОК БЫЛ ОБРЕЗАН В ТВОЕМ СООБЩЕНИИ (ОН НУЖЕН ЧТОБЫ КОД РАБОТАЛ)
        res_rw, res_gl = await asyncio.gather(
            supabase.get("/rest/v1/twitch_rewards", params={"order": "id.desc"}),
            supabase.get("/rest/v1/gift_logs", params={"order": "id.desc", "limit": "50"})
        )
        return {
            "channels": channels_metadata, 
            "rewards": res_rw.json() if res_rw.status_code == 200 else [],
            "gift_logs": res_gl.json() if res_gl.status_code == 200 else []
        }
    except Exception:
        return {"channels": [], "rewards": [], "gift_logs": []}
            
@app.post("/api/v1/admin/rewards/toggle")
async def toggle_admin_twitch_reward(id: int, status: bool, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    
    # 🔥 НОВОЕ: Выключаем награду прямо на Твиче
    r_resp = await supabase.get("/rest/v1/twitch_rewards", params={"id": f"eq.{id}", "select": "twitch_reward_id,broadcaster_id"})
    
    # Пуленепробиваемая проверка ответа базы
    if r_resp.status_code == 200:
        r_data = r_resp.json()
        
        # Проверяем, что вернулся именно список, а не словарь с ошибкой
        if r_data and isinstance(r_data, list) and r_data[0].get("twitch_reward_id") and r_data[0].get("broadcaster_id"):
            b_id = r_data[0]["broadcaster_id"]
            t_id = r_data[0]["twitch_reward_id"]
            
            t_resp = await supabase.get("/rest/v1/users", params={"twitch_id": f"eq.{b_id}", "select": "twitch_access_token"})
            
            # Проверяем ответ для токена тоже
            if t_resp.status_code == 200 and t_resp.json() and isinstance(t_resp.json(), list) and t_resp.json()[0].get("twitch_access_token"):
                b_token = t_resp.json()[0]["twitch_access_token"]
                url = f"https://api.twitch.tv/helix/channel_points/custom_rewards?broadcaster_id={b_id}&id={t_id}"
                headers = {"Authorization": f"Bearer {b_token}", "Client-Id": TWITCH_CLIENT_ID, "Content-Type": "application/json"}
                
                tw_res = await http_client.patch(url, headers=headers, json={"is_enabled": status})
                if tw_res.status_code not in [200, 204]:
                    logging.warning(f"Ошибка переключения награды на Twitch: {tw_res.text}")
    else:
        logging.error(f"Ошибка БД при поиске награды {id}: {r_resp.text}")

    # Обновляем статус в нашей базе
    db_res = await supabase.patch("/rest/v1/twitch_rewards", params={"id": f"eq.{id}"}, json={"is_active": status})
    if db_res.status_code not in [200, 204]:
        raise HTTPException(status_code=400, detail=f"Ошибка БД: {db_res.text}")
        
    return {"status": "ok"}

# ==============================================================================
# 📦 7. ЛОГИКА КОРОБОК И ГЕНЕРАЦИЯ (СВЕРХБЫСТРАЯ)
# ==============================================================================

class BoxCreateRequest(BaseModel): name: str; box_type: str = "nick_length"
class BoxGenerateRequest(BaseModel): min_price: float = 0.0; max_price: float = 10000.0; rarity: str = "all"
class SlotUpdateRequest(BaseModel): skin_name: str
class AddManualSlotRequest(BaseModel): box_id: int; slot_index: int; skin_name: str

def parse_condition(name: str):
    if "(Factory New)" in name or "(Прямо с завода)" in name: return "FN"
    if "(Minimal Wear)" in name or "(Немного поношенное)" in name: return "MW"
    if "(Field-Tested)" in name or "(После полевых испытаний)" in name: return "FT"
    if "(Well-Worn)" in name or "(Поношенное)" in name: return "WW"
    if "(Battle-Scarred)" in name or "(Закаленное в боях)" in name: return "BS"
    return "FN"

@app.get("/api/v1/admin/boxes")
async def get_admin_boxes(request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    res = await supabase.get("/rest/v1/reward_boxes", params={"select": "*,items:reward_box_items(count)", "order": "id.desc"})
    return res.json() if res.status_code == 200 else []

@app.post("/api/v1/admin/boxes/create")
async def create_admin_box(req: BoxCreateRequest, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    res = await supabase.post("/rest/v1/reward_boxes", json={"name": req.name, "box_type": req.box_type})
    if res.status_code in [200, 201, 204]: return {"status": "ok"}
    raise HTTPException(status_code=400, detail="Ошибка создания")

@app.post("/api/v1/admin/boxes/{box_id}/generate")
async def generate_box_content(box_id: int, req: BoxGenerateRequest, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    try:
        params = {"price_rub": f"gte.{req.min_price}", "and": f"(price_rub.lte.{req.max_price})", "is_available": "eq.true", "limit": "500"}
        if req.rarity and req.rarity != "all": params["rarity"] = f"eq.{req.rarity}"
            
        mc_res = await supabase.get("/rest/v1/market_cache", params=params)
        available_items = mc_res.json()
        if not available_items: raise HTTPException(status_code=400, detail="Пусто по фильтрам")

        import random
        selected_items = random.choices(available_items, k=30)
        box_items_payload, cs_items_to_insert = [], []
        
        for i, item in enumerate(selected_items):
            mhn = item.get("market_hash_name")
            box_items_payload.append({"box_id": box_id, "slot_index": i + 1, "skin_name": mhn, "chance_weight": 10})
            
            clean_name = mhn.split("(")[0].strip() if "(" in mhn else mhn
            cs_items_to_insert.append({
                "name": clean_name, "market_hash_name": mhn, "image_url": item.get("image_url", ""),
                "rarity": item.get("rarity", "common"), "condition": parse_condition(mhn),
                "price_rub": item.get("price_rub", 0.0), "price": item.get("price_rub", 0.0) / 100, "is_active": True
            })
            
        await supabase.post("/rest/v1/cs_items", json=cs_items_to_insert, headers={"Prefer": "resolution=ignore-duplicates"})
        await supabase.delete("/rest/v1/reward_box_items", params={"box_id": f"eq.{box_id}"})
        await supabase.post("/rest/v1/reward_box_items", json=box_items_payload)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/admin/boxes/{box_id}/items")
async def get_admin_box_items(box_id: int, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    
    res = await supabase.get("/rest/v1/reward_box_items", params={"box_id": f"eq.{box_id}", "order": "slot_index.asc"})
    items = res.json()
    if not items: return []
    
    # Собираем все уникальные названия скинов для одного SQL запроса
    skin_names = list(set([item["skin_name"] for item in items if item.get("skin_name")]))
    cache_map = {}
    
    if skin_names:
        # Паттерн "in.("Skin 1", "Skin 2")" для Supabase
        formatted_names = ",".join([f'"{name}"' for name in skin_names])
        mc_res = await supabase.get("/rest/v1/market_cache", params={"market_hash_name": f"in.({formatted_names})"})
        if mc_res.status_code == 200:
            cache_map = {c["market_hash_name"]: c for c in mc_res.json()}

    enriched_items = []
    for item in items:
        c_data = cache_map.get(item.get("skin_name"), {})
        enriched_items.append({
            "id": item.get("id"), "box_id": item.get("box_id"), "slot_index": item.get("slot_index"),
            "skin_name": item.get("skin_name"), "image_url": c_data.get("image_url") or item.get("image_url") or "",
            "price_rub": c_data.get("price_rub") or 0.0, "rarity": c_data.get("rarity") or "common",
            "condition": parse_condition(item.get("skin_name"))
        })
    return enriched_items

@app.post("/api/v1/admin/box_items/{item_id}/update")
async def update_admin_box_slot(item_id: int, req: SlotUpdateRequest, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    try:
        skin_name = req.skin_name.strip()
        mc_res = await supabase.get("/rest/v1/market_cache", params={"market_hash_name": f"eq.{skin_name}"})
        if mc_res.status_code != 200 or not mc_res.json():
            raise HTTPException(status_code=400, detail="Скин не найден")
            
        cache = mc_res.json()[0]
        clean_name = skin_name.split("(")[0].strip() if "(" in skin_name else skin_name
        price_rub = cache.get("price_rub", 0.0)
        
        cs_item_payload = {
            "name": clean_name, "market_hash_name": skin_name, "image_url": cache.get("image_url", ""),
            "rarity": cache.get("rarity", "common"), "condition": parse_condition(skin_name), "chance_weight": 10,
            "quantity": 1, "is_active": True, "boost_percent": 0.0, "price": price_rub / 100, "price_rub": price_rub
        }
        await supabase.post("/rest/v1/cs_items", json=cs_item_payload, headers={"Prefer": "resolution=ignore-duplicates"})
        await supabase.patch("/rest/v1/reward_box_items", params={"id": f"eq.{item_id}"}, json={"skin_name": skin_name})
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/admin/box_items/add_new_manual")
async def add_new_manual_skin_slot(req: AddManualSlotRequest, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    try:
        skin_name = req.skin_name.strip()
        mc_res = await supabase.get("/rest/v1/market_cache", params={"market_hash_name": f"eq.{skin_name}"})
        if mc_res.status_code != 200 or not mc_res.json(): raise HTTPException(status_code=400, detail="Скин не найден")
            
        cache = mc_res.json()[0]
        clean_name = skin_name.split("(")[0].strip() if "(" in skin_name else skin_name
        price_rub = cache.get("price_rub", 0.0)
        
        cs_item_payload = {
            "name": clean_name, "market_hash_name": skin_name, "image_url": cache.get("image_url", ""),
            "rarity": cache.get("rarity", "common"), "condition": parse_condition(skin_name), "chance_weight": 10,
            "quantity": 1, "is_active": True, "boost_percent": 0.0, "price": price_rub / 100, "price_rub": price_rub
        }
        await supabase.post("/rest/v1/cs_items", json=cs_item_payload, headers={"Prefer": "resolution=ignore-duplicates"})
        await supabase.post("/rest/v1/reward_box_items", json={"box_id": req.box_id, "slot_index": req.slot_index, "skin_name": skin_name})
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/twitch/fossabot_gift", response_class=PlainTextResponse)
async def handle_fossabot_gift(request: Request):
    token = request.headers.get("x-fossabot-customapitoken") or request.query_params.get("token")
    if not token: 
        return "ㅤ" # Невидимый символ от пустого спама

    try:
        # 1. Запрашиваем контекст из Fossabot
        fb_res = await http_client.get(f"https://api.fossabot.com/v2/customapi/context/{token}", timeout=3.0)
        if fb_res.status_code != 200: 
            return "❌ Ошибка связи с сервером Fossabot."
            
        msg_data = fb_res.json().get("message")
        if not msg_data: 
            return "ㅤ"

        twitch_login = msg_data["user"]["login"].lower()
        twitch_display = msg_data["user"]["display_name"]
        nick_length = len(twitch_login)

        # Подключаем наш глобальный клиент БД
        db = supabase_client
        target_box_id = 1 # 🔥 Добавили переменную для удобства

        # 2. ПАРАЛЛЕЛЬНО ищем юзера, его приз, статус коробки И анти-абуз (4 запроса сразу)
        user_task = db.get("/rest/v1/users", params={"twitch_login": f"eq.{twitch_login}", "select": "telegram_id, trade_link, is_banned"})
        prize_task = db.get("/rest/v1/reward_box_items", params={
            "box_id": f"eq.{target_box_id}", 
            "slot_index": f"eq.{nick_length}", 
            "select": "skin_name"
        })
        box_task = db.get("/rest/v1/reward_boxes", params={
            "id": f"eq.{target_box_id}",
            "select": "is_active"
        })
        # 🔥 НОВОЕ: Проверяем, забирал ли он уже награду
        claim_task = db.get("/rest/v1/box_players", params={
            "box_id": f"eq.{target_box_id}", 
            "twitch_login": f"eq.{twitch_login}", 
            "select": "id"
        })

        # Запускаем все ЧЕТЫРЕ запроса одновременно!
        user_res, prize_res, box_res, claim_res = await asyncio.gather(user_task, prize_task, box_task, claim_task)
        
        user_data = user_res.json() if user_res.status_code == 200 else []
        prize_data = prize_res.json() if prize_res.status_code == 200 else []
        box_data = box_res.json() if box_res.status_code == 200 else []
        claim_data = claim_res.json() if claim_res.status_code == 200 else []

        # ==========================================
        # 🛑 АНТИ-АБУЗ: ПРОВЕРКА НА ПОВТОР
        # ==========================================
        if claim_data:
            return f"🛑 @{twitch_display}, ты уже забирал свой приз с этой раздачи! Жди следующих 🐸"

        # ==========================================
        # 🛑 ПРОВЕРКА АКТИВНОСТИ НАГРАДЫ
        # ==========================================
        is_box_active = box_data[0].get("is_active", False) if box_data else False
        
        if not is_box_active:
            # Если в БД is_active = false, бот красиво разворачивает зрителя
            return f"🔒 @{twitch_display}, раздача подарков временно отключена! Следи за стримом, скоро включим обратно 🐸"

        prize_name = prize_data[0]['skin_name'] if prize_data else "Секретный скин"

        # ==========================================
        # СЦЕНАРИЙ 1: ПОЛЬЗОВАТЕЛЬ НОВИЧОК (НЕТ В ЛАВКЕ)
        # ==========================================
        if not user_data:
            return (f"👋 @{twitch_display}, в твоем нике {nick_length} символов! "
                    f"Твой приз: {prize_name}. "
                    f"Авторизуйся в нашем TG-боте и привяжи Twitch, чтобы забрать его в профиль!")

        # ==========================================
        # СЦЕНАРИЙ 2: ЮЗЕР ЕСТЬ В ЛАВКЕ
        # ==========================================
        user_info = user_data[0]
        user_tg_id = user_info.get("telegram_id")
        trade_link = user_info.get("trade_link")
        is_banned = user_info.get("is_banned")

        # 🛡️ Защита от забаненных юзеров
        if is_banned:
            return f"🚫 @{twitch_display}, ваш аккаунт заблокирован в системе."

        # Проверка трейд-ссылки
        if not trade_link:
            return (f"⚠️ @{twitch_display}, ты есть в лавке, но не привязал трейд-ссылку! "
                    f"Добавь её в TG-боте, чтобы получить {prize_name}.")

        # Ищем ID предмета в каталоге
        item_res = await db.get("/rest/v1/cs_items", params={
            "market_hash_name": f"eq.{prize_name}", 
            "select": "id", 
            "limit": 1
        })
        item_data = item_res.json()
        item_id = item_data[0]['id'] if (item_data and len(item_data) > 0) else None

        # 3. ДОБАВЛЯЕМ В ИНВЕНТАРЬ И ФИКСИРУЕМ В BOX_PLAYERS (ПАРАЛЛЕЛЬНО)
        # 🔥 НОВОЕ: Запускаем два POST-запроса одновременно
        await asyncio.gather(
            db.post("/rest/v1/cs_history", json={
                "user_id": user_tg_id, 
                "item_id": item_id,
                "status": "available", 
                "case_name": "Подарок со стрима",
                "details": f"Выигрыш: {prize_name}",
                "source": "twitch",
                "is_swapped": False
            }),
            db.post("/rest/v1/box_players", json={
                "box_id": target_box_id,
                "telegram_id": user_tg_id,
                "twitch_login": twitch_login
            })
        )

        print(f"✅ Подарок {prize_name} добавлен в инвентарь для TG ID {user_tg_id} и зафиксирован анти-абуз.")
        
        # 4. Мгновенно отвечаем в Twitch чат
        return (f"🎉 @{twitch_display}, твой ник = {nick_length} символов! "
                f"Выдаю {prize_name}. Предмет уже лежит в твоем инвентаре в ТГ-боте, можешь выводить!")

    except Exception as e:
        print(f"Fossabot Gift Error: {e}")
        return "ㅤ" # Молчим при ошибке

# ==============================================================================
# 🎟️ РОЗЫГРЫШИ (RAFFLES) ДЛЯ НОВИЧКОВ
# ==============================================================================

@app.get("/raffles", response_class=HTMLResponse)
async def raffles_page(request: Request):
    token = request.cookies.get("admin_session")
    if not token: return RedirectResponse(url="/")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return HTMLResponse(content=get_html("raffles.html").replace("{{USERNAME}}", payload.get('login', 'Admin')))
    except jwt.PyJWTError:
        return RedirectResponse(url="/")

class RaffleCreateRequest(BaseModel):
    title: str
    cost: int
    broadcaster_id: str
    prize_name: str
    prize_price: float
    duration_minutes: int
    steps: Optional[list] = []  # 🔥 НОВОЕ ПОЛЕ

@app.post("/api/v1/admin/raffles/create")
async def create_admin_raffle(req: RaffleCreateRequest, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    if not QSTASH_TOKEN or not WEB_APP_URL:
        raise HTTPException(status_code=500, detail="QSTASH_TOKEN или WEB_APP_URL не настроены в Vercel!")
        
    # 1. Достаем токен стримера
    token_res = await supabase.get("/rest/v1/users", params={"twitch_id": f"eq.{req.broadcaster_id}", "select": "twitch_access_token"})
    token_data = token_res.json()
    if not token_data or not token_data[0].get("twitch_access_token"):
        raise HTTPException(status_code=400, detail="Токен стримера не найден.")
    broadcaster_token = token_data[0]["twitch_access_token"]

    # 2. Создаем награду на Twitch. ЖЕСТКО требуем ввод текста (для трейд-ссылки)
    twitch_reward_id = None
    twitch_url = f"https://api.twitch.tv/helix/channel_points/custom_rewards?broadcaster_id={req.broadcaster_id}"
    headers = {"Authorization": f"Bearer {broadcaster_token}", "Client-Id": TWITCH_CLIENT_ID, "Content-Type": "application/json"}
    
    tw_res = await http_client.post(twitch_url, headers=headers, json={
        "title": req.title,
        "cost": req.cost,
        "is_user_input_required": True, # Обязательно для трейд-ссылки!
        "background_color": "#E0115F" # Красивый цвет для розыгрышей
    })
    
    twitch_reward_id = None
    if tw_res.status_code == 400 and "DUPLICATE_REWARD" in tw_res.text:
        # 🔥 Перехват: Ищем старую награду на Твиче и берем её ID
        get_url = f"https://api.twitch.tv/helix/channel_points/custom_rewards?broadcaster_id={req.broadcaster_id}&only_manageable_rewards=true"
        get_res = await http_client.get(get_url, headers=headers)
        if get_res.status_code == 200:
            for r in get_res.json().get("data", []):
                if r.get("title") == req.title:
                    twitch_reward_id = r.get("id")
                    break
                    
        if twitch_reward_id:
            # Обновляем старую награду: ставим новую цену и включаем
            patch_url = f"https://api.twitch.tv/helix/channel_points/custom_rewards?broadcaster_id={req.broadcaster_id}&id={twitch_reward_id}"
            await http_client.patch(patch_url, headers=headers, json={
                "cost": req.cost, "is_user_input_required": True, "background_color": "#E0115F", "is_enabled": True
            })
        else:
            raise HTTPException(status_code=400, detail=f"Награда '{req.title}' уже существует на Twitch, но бот не имеет прав на её изменение. Удали её вручную на Twitch.")
            
    elif tw_res.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Ошибка Twitch: {tw_res.text}")
    else:
        twitch_reward_id = tw_res.json()["data"][0]["id"]

    # 3. Сохраняем награду в нашу БД twitch_rewards (С ЗАЩИТОЙ ОТ ДУБЛЕЙ)
    rw_payload = {
        "title": req.title,
        "reward_type": "raffle",
        "broadcaster_id": req.broadcaster_id,
        "cost": req.cost,
        "twitch_reward_id": twitch_reward_id,
        "is_active": True,
        "steam_item_name": req.prize_name,
        "show_user_input": True,
        "condition_type": "none"
    }
    
    # Ищем старую награду
    check_db = await supabase.get("/rest/v1/twitch_rewards", params={"title": f"eq.{req.title}", "select": "id"})
    
    if check_db.status_code == 200 and check_db.json():
        existing_id = check_db.json()[0]["id"]
        db_rw = await supabase.patch("/rest/v1/twitch_rewards", params={"id": f"eq.{existing_id}"}, json=rw_payload, headers={"Prefer": "return=representation"})
    else:
        db_rw = await supabase.post("/rest/v1/twitch_rewards", json=rw_payload, headers={"Prefer": "return=representation"})
    
    if db_rw.status_code not in [200, 201]:
        raise HTTPException(status_code=400, detail="Ошибка создания награды в БД")
    
    internal_reward_id = db_rw.json()[0]["id"]

    # 4. Создаем запись о Розыгрыше (raffles)
    start_time = datetime.now(timezone.utc)
    end_time = start_time + timedelta(minutes=req.duration_minutes)
    
    raf_payload = {
        "title": req.title,
        "type": "twitch_direct", # Специальный тип для розыгрышей новичков
        "status": "active",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "settings": {
            "required_twitch_reward_id": internal_reward_id,
            "prize_name": req.prize_name,
            "prize_price": req.prize_price,
            "duration_minutes": req.duration_minutes
        }
    }
    db_raf = await supabase.post("/rest/v1/raffles", json=raf_payload, headers={"Prefer": "return=representation"})
    
    if db_raf.status_code not in [200, 201]:
        raise HTTPException(status_code=400, detail=f"Ошибка БД: {db_raf.text}")
        
    raffle_id = db_raf.json()[0]["id"]

    # 5. Отправляем задачу в QStash
    delay_seconds = req.duration_minutes * 60
    target_webhook_url = f"{WEB_APP_URL.rstrip('/')}/api/raffles/twitch-direct/{raffle_id}/finalize"
    
    qstash_headers = {
        "Authorization": f"Bearer {QSTASH_TOKEN}",
        "Upstash-Delay": f"{delay_seconds}s",
        "Content-Type": "application/json"
    }
    
    q_res = await http_client.post(
        f"https://qstash.upstash.io/v2/publish/{target_webhook_url}",
        headers=qstash_headers,
        json={} # Можно передать пустое тело, id уже в URL
    )
    
    if q_res.status_code not in [200, 201]:
        logging.error(f"Ошибка QStash: {q_res.text}")
        # Розыгрыш создан, но таймер не завелся - придется закрывать руками
        
    return {"status": "success", "raffle_id": raffle_id}

from fastapi import HTTPException

# Используем @app.get вместо @router.get и прописываем полный путь
@app.get("/api/v1/admin/raffles/{raffle_id}/participants")
async def get_raffle_participants(
    raffle_id: int, 
    request: Request, 
    supabase: httpx.AsyncClient = Depends(get_supabase_client)
):
    # Опционально: защита эндпоинта, как и везде в админке
    if not request.cookies.get("admin_session"):
        raise HTTPException(status_code=401)

    try:
        # Используем PostgREST параметры для прямого HTTP запроса
        params = {
            "raffle_id": f"eq.{raffle_id}",
            "select": "*,users(full_name,twitch_login)",
            "order": "score.desc"
        }
        
        response = await supabase.get("/rest/v1/raffle_participants", params=params)
        
        if response.status_code == 200:
            return response.json()
            
        raise HTTPException(status_code=response.status_code, detail=response.text)
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/v1/admin/raffles/{raffle_id}")
async def delete_admin_raffle(raffle_id: int, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): 
        raise HTTPException(status_code=401)
    
    # 1. Получаем инфу о розыгрыше
    raf_res = await supabase.get("/rest/v1/raffles", params={"id": f"eq.{raffle_id}", "select": "type, settings"})
    if raf_res.status_code == 200 and raf_res.json():
        raffle = raf_res.json()[0]
        settings = raffle.get("settings", {})
        internal_reward_id = settings.get("required_twitch_reward_id")
        
        # 2. Если это Twitch-розыгрыш, удаляем награду с самого Twitch и из таблицы twitch_rewards
        if internal_reward_id:
            rew_res = await supabase.get("/rest/v1/twitch_rewards", params={"id": f"eq.{internal_reward_id}", "select": "broadcaster_id, twitch_reward_id"})
            if rew_res.status_code == 200 and rew_res.json():
                b_id = rew_res.json()[0].get("broadcaster_id")
                t_id = rew_res.json()[0].get("twitch_reward_id")
                
                # Идем на Twitch удалять награду
                if b_id and t_id:
                    tok_res = await supabase.get("/rest/v1/users", params={"twitch_id": f"eq.{b_id}", "select": "twitch_access_token"})
                    if tok_res.status_code == 200 and tok_res.json():
                        b_token = tok_res.json()[0].get("twitch_access_token")
                        if b_token:
                            twitch_url = f"https://api.twitch.tv/helix/channel_points/custom_rewards?broadcaster_id={b_id}&id={t_id}"
                            await http_client.delete(twitch_url, headers={"Authorization": f"Bearer {b_token}", "Client-Id": TWITCH_CLIENT_ID})
                            
            # Удаляем саму награду из БД
            await supabase.delete("/rest/v1/twitch_rewards", params={"id": f"eq.{internal_reward_id}"})
            
    # 3. Удаляем сам розыгрыш (Участники удалятся автоматически благодаря CASCADE в БД)
    res = await supabase.delete("/rest/v1/raffles", params={"id": f"eq.{raffle_id}"})
    if res.status_code in [200, 204]:
        return {"status": "success"}
        
    raise HTTPException(status_code=400, detail="Ошибка удаления из базы данных")

@app.get("/api/v1/admin/raffles/list")
async def get_admin_raffles(request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): 
        raise HTTPException(status_code=401)
    
    # 1. Получаем список розыгрышей
    res = await supabase.get("/rest/v1/raffles", params={"order": "id.desc", "limit": "100"})
    if res.status_code != 200:
        return []
        
    raffles = res.json()
    if not raffles:
        return []

    # 2. Собираем уникальные названия скинов.
    # .split(" (")[0] отсечет износ, если ты его случайно написал при создании (например "AK-47 | Redline (FT)")
    titles = list(set(r.get("title").split(" (")[0] for r in raffles if r.get("title")))
    
    if titles:
        search_names = []
        # Наиболее частые износы в Steam + пустая строка (для наклеек и кейсов)
        wears = [
            "", 
            " (Field-Tested)", 
            " (Minimal Wear)", 
            " (Factory New)", 
            " (Well-Worn)", 
            " (Battle-Scarred)"
        ]
        
        # Генерируем точные имена для Primary Key (в разы быстрее любого поиска)
        for t in titles:
            for w in wears:
                search_names.append(f'"{t}{w}"')
        
        images_map = {}
        
        # Разбиваем на порции по 150 штук, чтобы не перегрузить длину URL-адреса
        chunk_size = 150
        for i in range(0, len(search_names), chunk_size):
            chunk = search_names[i:i+chunk_size]
            names_str = ",".join(chunk)
            
            # Бьем прямо в индекс Primary Key. Лимиты тут не нужны.
            cache_res = await supabase.get(
                "/rest/v1/market_cache", 
                params={
                    "select": "market_hash_name,image_url",
                    "market_hash_name": f"in.({names_str})"
                }
            )
            
            if cache_res.status_code == 200:
                for item in cache_res.json():
                    if item.get("image_url"):
                        # Отрезаем износ у найденного скина, чтобы получить базовое имя
                        base_name = item["market_hash_name"].split(" (")[0]
                        # Сохраняем первую найденную картинку
                        if base_name not in images_map:
                            images_map[base_name] = item["image_url"]
                            
        # 3. Раздаем картинки нашим розыгрышам
        for r in raffles:
            if not r.get("image_url") and r.get("title"):
                base_title = r["title"].split(" (")[0]
                if base_title in images_map:
                    r["image_url"] = images_map[base_title]

    return raffles

# ==============================================================================
# 🎥 OBS ВИДЖЕТЫ
# ==============================================================================

@app.get("/obs/raffle/{raffle_id}", response_class=HTMLResponse)
async def obs_raffle_page(raffle_id: int):
    # Отдаем чистую HTML страницу для OBS
    return HTMLResponse(content=get_html("obs_raffle.html"))

@app.get("/api/v1/obs/raffle/{raffle_id}/data")
async def get_obs_raffle_data(raffle_id: int, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    # Быстрый эндпоинт для обновления данных в реальном времени (без авторизации, т.к. это для OBS)
    res = await supabase.get("/rest/v1/raffles", params={
        "id": f"eq.{raffle_id}",
        "select": "title, participants_count, settings, status"
    })
    
    if res.status_code != 200 or not res.json():
        raise HTTPException(status_code=404, detail="Not found")
        
    return res.json()[0]

# =========================================================================
# ⚙️ 2. СКРЫТЫЙ ЭНДПОИНТ-ВОРКЕР (Спокойно закупает скин за 10 секунд)
# =========================================================================
from pydantic import BaseModel
from typing import Optional

class WorkerPayload(BaseModel):
    user_id: int
    target_name: str
    target_price_rub: float
    trade_url: str
    history_id: int
    source: Optional[str] = "shop" # По умолчанию магазин
    secret_token: str              # 🔥 Секретный ключ для защиты

import os
import logging

INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "super-secret-key-123") # Задай в Vercel

@app.post("/api/v1/internal/worker_buy_skin")
async def worker_buy_skin(payload: WorkerPayload):
    # 1. Защита эндпоинта
    if payload.secret_token != INTERNAL_API_SECRET:
        logging.warning(f"⚠️ Попытка несанкционированного доступа к воркеру! Юзер: {payload.user_id}")
        return {"status": "error", "message": "Unauthorized"}

    db = await get_background_client() 

    try:
        logging.info(f"⚙️ Воркер начал работу: Закупка {payload.target_name} для юзера {payload.user_id} (Источник: {payload.source})...")
        
        await fulfill_item_delivery(
            user_id=payload.user_id,
            target_name=payload.target_name,
            target_price_rub=payload.target_price_rub,
            trade_url=payload.trade_url,
            supabase=db,
            history_id=payload.history_id,
            source=payload.source  # 🔥 Берем из payload, а не хардкодим
        )
        logging.info(f"✅ Воркер успешно отработал ордер #{payload.history_id}")
        return {"status": "ok"}
        
    except Exception as e:
        logging.error(f"❌ Воркер сломался на ордере #{payload.history_id}: {e}")
        
        # 🔥 Откатываем статус предмета, чтобы он не завис навсегда, 
        # и пользователь мог вывести его позже вручную
        if db:
            try:
                await db.patch(
                    "/rest/v1/cs_history",
                    params={"id": f"eq.{payload.history_id}"},
                    json={"status": "available"}
                )
                logging.info(f"🔄 Статус ордера #{payload.history_id} откачен на 'available'")
            except Exception as db_err:
                logging.error(f"🚨 Не удалось откатить статус ордера #{payload.history_id}: {db_err}")

        return {"status": "error"}

@app.post("/api/v1/admin/boxes/toggle")
async def toggle_admin_box(
    id: int, 
    status: bool, 
    request: Request, 
    supabase: httpx.AsyncClient = Depends(get_supabase_client)
):
    if not request.cookies.get("admin_session"): 
        raise HTTPException(status_code=401)
        
    try:
        await supabase.patch("/rest/v1/reward_boxes", params={"id": f"eq.{id}"}, json={"is_active": status})
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.delete("/api/v1/admin/boxes/players/{player_id}")
async def delete_box_player(player_id: int, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    try:
        await supabase.delete("/rest/v1/box_players", params={"id": f"eq.{player_id}"})
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/admin/boxes/{box_id}/players")
async def get_box_players(
    box_id: int, 
    request: Request, 
    search: str = "", 
    supabase: httpx.AsyncClient = Depends(get_supabase_client)
):
    if not request.cookies.get("admin_session"): 
        raise HTTPException(status_code=401)
    
    try:
        params = {"box_id": f"eq.{box_id}", "order": "id.desc"}
        if search:
            # Умный поиск: ищем и по Twitch логину, и по TG ID
            params["or"] = f"(twitch_login.ilike.*{search.strip()}*,telegram_id.eq.{search.strip() if search.strip().isdigit() else 0})"
        
        res = await supabase.get("/rest/v1/box_players", params=params)
        return res.json() if res.status_code == 200 else []
    except Exception:
        return []

# ==============================================================================
# 🛒 8. ИНТЕГРАЦИЯ CS MARKET И CRON ДЛЯ НОВИЧКОВ
# ==============================================================================

class MarketCSGO:
    def __init__(self, api_key: str, use_proxy: bool = True):
        self.api_key = api_key
        self.base_url = "https://cs2.market/api/v2" 
        self.use_proxy = use_proxy

    @staticmethod
    def parse_trade_link(trade_link: str):
        try:
            trade_link = trade_link.strip()
            parsed_url = urllib.parse.urlparse(trade_link)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            raw_partner = query_params.get('partner', [None])[0]
            raw_token = query_params.get('token', [None])[0]
            
            partner = re.sub(r'\D', '', str(raw_partner)) if raw_partner else None
            token = re.sub(r'[^a-zA-Z0-9_-]', '', str(raw_token)) if raw_token else None
            
            if partner and token:
                return partner, token
            return None, None
        except Exception:
            return None, None

    async def _make_request(self, endpoint: str, params: dict = None) -> dict:
        if params is None: params = {}
        params['key'] = self.api_key
        
        query_string = "&".join([f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items()])
        url = f"{self.base_url}/{endpoint}?{query_string}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"'
        }

        PROXY_URL = "http://HatelovestreamertO5:bf0127fM6@node-ru-229.astroproxy.com:10065"
        custom_timeout = httpx.Timeout(15.0, connect=10.0)

        client_args = {"headers": headers}
        if self.use_proxy:
            client_args["proxy"] = PROXY_URL

        async with httpx.AsyncClient(**client_args) as client:
            try:
                response = await client.get(url, timeout=custom_timeout)
                if response.status_code == 200:
                    return response.json()
                return {"success": False, "error": f"status_{response.status_code}"}
            except httpx.ConnectTimeout:
                logging.warning("[MARKET] Мертвый IP прокси (ConnectTimeout).")
                return {"success": False, "error": "timeout_limit"}
            except httpx.ReadTimeout:
                logging.warning("[MARKET] Маркет долго думает (ReadTimeout).")
                return {"success": False, "error": "timeout_limit"}
            except Exception as e:
                logging.warning(f"[MARKET] Сбой: {e}.")
                return {"success": False, "error": "timeout_limit"}

    # 🔥 НОВАЯ ФУНКЦИЯ: Поиск реальной минимальной цены прямо сейчас
    async def get_lowest_price(self, hash_name: str) -> Optional[float]:
        try:
            response = await self._make_request("search-item-by-hash-name", {"hash_name": hash_name})
            if isinstance(response, dict) and response.get("success") and response.get("data"):
                items = response["data"]
                if items:
                    # Маркет всегда отдает список от дешевых к дорогим. Берем [0]
                    cheapest = items[0]
                    return float(cheapest.get("price", 0)) / 100 # переводим копейки в рубли
        except Exception as e:
            logging.error(f"[MARKET] Ошибка при поиске минимальной цены: {e}")
        return None

    async def buy_for_user(self, hash_name: str, max_price_rub: float, trade_link: str, custom_id: str): 
        partner, token = self.parse_trade_link(trade_link)
        if not partner or not token:
            return {"success": False, "error": "Неверная трейд-ссылка"}

        # 🔥 1. Узнаем РЕАЛЬНУЮ минимальную цену на маркете
        real_lowest_price = await self.get_lowest_price(hash_name)
        
        if real_lowest_price:
            # Делаем потолок: минимальная цена + 5% (чтобы точно выкупить, если первый лот уведут из-под носа)
            ceiling_rub = real_lowest_price * 1.05
            
            # 🔥 БРОНЕЖИЛЕТ ОТ СЛИВА ДЕНЕГ:
            # Если цена на маркете внезапно стала больше нашей ожидаемой (max_price_rub) в 2+ раза — отменяем!
            if max_price_rub > 0 and ceiling_rub > (max_price_rub * 2.0):
                error_msg = f"Скин резко подорожал (Ожидали ~{max_price_rub}₽, сейчас минимум {real_lowest_price}₽). Покупка отменена."
                logging.warning(f"[RAFFLE] {error_msg}")
                return {"success": False, "error": error_msg}
                
            logging.info(f"[MARKET] Найден дешевый '{hash_name}' за {real_lowest_price:.2f} руб. Бронируем покупку.")
        else:
            # Если маркет лагает и не отдал поиск, используем безопасный резервный потолок
            logging.warning(f"[MARKET] Не удалось узнать текущую цену '{hash_name}'. Используем резервный лимит.")
            if max_price_rub <= 20:
                ceiling_rub = max_price_rub * 3.0 
            elif max_price_rub <= 100:
                ceiling_rub = max_price_rub * 2.0
            else:
                ceiling_rub = max_price_rub * 1.3

        # Переводим в копейки для API
        price_in_kopecks = int(ceiling_rub * 100)

        params = {
            "hash_name": hash_name,
            "price": price_in_kopecks, 
            "partner": partner,
            "token": token,
            "custom_id": custom_id
        }
        
        response = await self._make_request("buy-for", params)
        
        if isinstance(response, dict) and not response.get("success") and "error" in response:
            err_str = response.get("error", "")
            if err_str.startswith("status_"):
                err_code = int(err_str.split("_")[1])
                return {"success": False, "error": f"Маркет недоступен (HTTP {err_code})", "code": err_code}
            elif err_str == "timeout_limit":
                return {"success": False, "error": "Маркет завис (Таймаут)", "code": 504}

        response['custom_id'] = custom_id 
        return response

# ==============================================================================
# 📡 WEBHOOK ОТ SUPABASE: АВТО-УПРАВЛЕНИЕ CRON-JOB ПРИ СМЕНЕ СТАТУСА
# ==============================================================================

@app.post("/api/v1/internal/supabase_webhook")
async def supabase_stream_status_webhook(
    request: Request, 
    webhook_secret: str = "", 
    supabase: httpx.AsyncClient = Depends(get_supabase_client)
):
    """Сюда бьет Supabase каждый раз, когда меняется таблица settings"""
    
    # 1. Защита от чужих запросов
    expected_secret = os.getenv("WEBHOOK_SECRET", "HateLavkaSecretKey")
    if webhook_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Неверный секретный ключ")
        
    try:
        payload = await request.json()
        record = payload.get("record", {})
        key = record.get("key")
        
        # 2. Если изменили не статус стрима — игнорим и не тратим ресурсы
        if key not in ["twitch_status_883996654", "twitch_status_755238101"]:
            return {"status": "ignored", "message": "Изменен другой ключ"}
            
        # 3. Важная проверка: проверяем ОБА стрима. 
        # Вдруг один выключили, а второй еще идет? Крон должен работать!
        set_res = await supabase.get("/rest/v1/settings", params={"key": "in.(twitch_status_883996654,twitch_status_755238101)"})
        
        any_online = False
        if set_res.status_code == 200:
            for s in set_res.json():
                val = s.get("value")
                if val is True or val == "true" or val == True:
                    any_online = True
                    break
                    
        # 4. Управляем cron-job.org
        cron_api_key = os.getenv("CRON_API")
        job_id = os.getenv("CRON_JOB_ID")
        
        if not cron_api_key or not job_id:
            logging.error("Нет ключей CRON_API или CRON_JOB_ID в Vercel")
            return {"status": "error", "message": "Ключи не настроены"}
            
        # Отправляем PATCH запрос к API cron-job.org
        url = f"https://api.cron-job.org/jobs/{job_id}"
        headers = {
            "Authorization": f"Bearer {cron_api_key}",
            "Content-Type": "application/json"
        }
        
        # Если any_online=True, задача включится. Если False - выключится
        cron_payload = {"job": {"enabled": any_online}}
        cron_res = await http_client.patch(url, headers=headers, json=cron_payload)
        
        if cron_res.status_code == 200:
            state = "ВКЛЮЧЕН 🟢" if any_online else "ВЫКЛЮЧЕН 🔴"
            logging.info(f"Supabase Webhook сработал: {key}. Крон {state}!")
            return {"status": "ok", "cron_state": any_online}
        else:
            logging.error(f"Ошибка cron-job.org: {cron_res.text}")
            return {"status": "error", "message": "cron-job API error"}
            
    except Exception as e:
        logging.error(f"Сбой в вебхуке Supabase: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/v1/cron/process_newbies")
@app.post("/api/v1/cron/process_newbies")
async def process_newbies_cron(request: Request, cron_secret: Optional[str] = None, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    expected_secret = os.getenv("CRON_SECRET", "HateLavkaSecretKey")
    if cron_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # ИЩЕМ И СТАТУС "Привязан" ТОЖЕ!
    res = await supabase.get("/rest/v1/twitch_reward_purchases", params={
        "status": "in.(Не привязан,Ожидает выдачи,Привязан)", 
        "limit": 10,
        "order": "id.asc"
    })
    
    if res.status_code != 200 or not res.json():
        return {"status": "ok", "message": "Нет заявок"}
        
    purchases = res.json()
    
    for p in purchases:
        p_id = p["id"]
        reward_id = p["reward_id"]
        purchaser_login = p.get("twitch_login", "").lower()
        
        # 🔥 Получаем то, что ввел юзер, и ID транзакции Twitch для возврата
        user_input = p.get("user_input", "").strip()
        twitch_redemption_id = p.get("twitch_redemption_id") 
        
        # Лочим заявку
        await supabase.patch("/rest/v1/twitch_reward_purchases", params={"id": f"eq.{p_id}"}, json={"status": "В обработке"})
        
        # Получаем инфу о награде (добавили выборку broadcaster_id и twitch_reward_id)
        rew_res = await supabase.get("/rest/v1/twitch_rewards", params={"id": f"eq.{reward_id}", "select": "title, steam_item_name, reward_type, broadcaster_id, twitch_reward_id"})
        if not rew_res.json(): 
            await supabase.patch("/rest/v1/twitch_reward_purchases", params={"id": f"eq.{p_id}"}, json={"status": "Ошибка: Награда удалена"})
            continue
            
        reward_data = rew_res.json()[0]
        reward_type = reward_data.get("reward_type")
        broadcaster_id = reward_data.get("broadcaster_id")
        twitch_reward_id = reward_data.get("twitch_reward_id")
        
        # СЦЕНАРИЙ А: ЭТО РОЗЫГРЫШ
        if reward_type == "raffle":
            
            # 🔥 1. ЗАРАНЕЕ ДОСТАЕМ ЮЗЕРА И ПРОВЕРЯЕМ ЕГО БАЗОВУЮ ССЫЛКУ
            tg_id = None
            has_db_link = False
            if purchaser_login:
                u_res = await supabase.get("/rest/v1/users", params={"twitch_login": f"eq.{purchaser_login}", "select": "telegram_id, trade_link"})
                if u_res.status_code == 200 and u_res.json():
                    tg_id = u_res.json()[0].get("telegram_id")
                    t_link = u_res.json()[0].get("trade_link")
                    if t_link and len(t_link) > 10:
                        has_db_link = True
            
            # =====================================================================
            # 🛡 АНТИ-АБУЗ: Проверка на наличие трейд-ссылки (где угодно)
            import re
            is_valid_link = bool(re.search(r"partner=\d+&token=[a-zA-Z0-9_-]+", user_input))
            
            # 🚨 ПРАВИЛО: Если у юзера НЕТ ссылки в базе И он НЕ вставил валидную ссылку в текст награды
            if not has_db_link and not is_valid_link:
                try:
                    if twitch_redemption_id and broadcaster_id:
                        tok_res = await supabase.get("/rest/v1/users", params={"twitch_id": f"eq.{broadcaster_id}", "select": "twitch_access_token"})
                        if tok_res.status_code == 200 and tok_res.json():
                            b_token = tok_res.json()[0].get("twitch_access_token")
                            if b_token:
                                headers = {"Authorization": f"Bearer {b_token}", "Client-Id": TWITCH_CLIENT_ID, "Content-Type": "application/json"}
                                
                                # 1. ВОЗВРАЩАЕМ БАЛЛЫ
                                refund_url = f"https://api.twitch.tv/helix/channel_points/custom_rewards/redemptions?broadcaster_id={broadcaster_id}&reward_id={t_reward_id}&id={twitch_redemption_id}"
                                await http_client.patch(refund_url, headers=headers, json={"status": "CANCELED"})
                                logging.info(f"✅ [АНТИ-АБУЗ] Баллы возвращены юзеру {purchaser_login} (Нет трейд-ссылки ни в БД, ни в тексте)")
                                
                                # 2. ПИШЕМ ЧЕТКОЕ СООБЩЕНИЕ В ЧАТ
                                try:
                                    chat_url = "https://api.twitch.tv/helix/chat/messages"
                                    chat_msg = f"@{purchaser_login}, у тебя нет привязанной трейд-ссылки! Для участия ОБЯЗАТЕЛЬНО ВСТАВЬ СВОЮ ТРЕЙД-ССЫЛКУ прямо в текст награды. Твои баллы возвращены 🔄"
                                        
                                    await http_client.post(chat_url, headers=headers, json={
                                        "broadcaster_id": broadcaster_id,
                                        "sender_id": broadcaster_id,
                                        "message": chat_msg
                                    })
                                except Exception as e:
                                    logging.error(f"⚠️ Ошибка отправки в чат: {e}")
                except Exception as e:
                    logging.error(f"🚨 Ошибка возврата баллов: {e}")
                
                # Закрываем заявку в нашей БД со статусом отмены
                await supabase.patch("/rest/v1/twitch_reward_purchases", params={"id": f"eq.{p_id}"}, json={"status": "Отмена: Нет трейд-ссылки"})
                continue
            # =====================================================================

            # Ищем активный розыгрыш
            raf_res = await supabase.get("/rest/v1/raffles", params={
                "status": "eq.active",
                "settings->>required_twitch_reward_id": f"eq.{reward_id}",
                "select": "id, participants_count, settings",
                "limit": 1
            })
            
            if not raf_res.json():
                await supabase.patch("/rest/v1/twitch_reward_purchases", params={"id": f"eq.{p_id}"}, json={"status": "Ошибка: Розыгрыш не найден/завершен"})
                continue
                
            raffle_id = raf_res.json()[0]["id"]
            current_count = raf_res.json()[0].get("participants_count", 0)
            
            # Добавляем в таблицу участников (tg_id мы уже нашли на самом верху!)
            await supabase.post("/rest/v1/raffle_participants", json={
                "raffle_id": raffle_id,
                "user_id": tg_id,
                "source": "twitch",
                "score": 1
            }, headers={"Prefer": "resolution=ignore-duplicates"})
            
            # Обновляем счетчик
            await supabase.patch("/rest/v1/raffles", params={"id": f"eq.{raffle_id}"}, json={"participants_count": current_count + 1})
            
            # Проверяем, не пора ли апгрейднуть приз (Ступенчатая система)
            raffle_settings = raf_res.json()[0].get("settings", {})
            await check_and_upgrade_raffle_prize(supabase, raffle_id, current_count + 1, raffle_settings)
            
            # Статус покупки -> Участвует
            await supabase.patch("/rest/v1/twitch_reward_purchases", params={"id": f"eq.{p_id}"}, json={
                "status": "Участвует", 
                "rewarded_at": datetime.now(timezone.utc).isoformat(),
                "viewed_by_admin": True,
                "viewed_by_admin_name": "Авто-регистрация"
            })
        else:
            # ЕСЛИ ЭТО НЕ РОЗЫГРЫШ - просто игнорируем или ставим статус
            await supabase.patch("/rest/v1/twitch_reward_purchases", params={"id": f"eq.{p_id}"}, json={"status": "Игнорировано (не raffle)"})

    return {"status": "ok"}
    
class CleanupRequest(BaseModel):
    start_date: str
    end_date: str

@app.get("/api/v1/admin/purchases")
async def get_admin_purchases(request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    res = await supabase.get("/rest/v1/twitch_reward_purchases", params={"order": "id.desc", "limit": "100"})
    return res.json() if res.status_code == 200 else []

@app.delete("/api/v1/admin/purchases/cleanup")
async def cleanup_purchases(req: CleanupRequest, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    
    res = await supabase.delete("/rest/v1/twitch_reward_purchases", params={
        "created_at": f"gte.{req.start_date}T00:00:00Z",
        "and": f"(created_at.lte.{req.end_date}T23:59:59Z)"
    })
    
    if res.status_code in [200, 204]:
        return {"status": "ok"}
    raise HTTPException(status_code=400, detail=f"Ошибка БД: {res.text}")
