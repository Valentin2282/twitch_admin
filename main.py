import os
import httpx
import jwt
import pathlib
import time
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Request, Response, Depends, BackgroundTasks
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
    url = (
        f"https://id.twitch.tv/oauth2/authorize?response_type=code"
        f"&client_id={TWITCH_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope=user:read:email+channel:read:redemptions"
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

@app.get("/api/v1/auth/logout")
async def logout():
    redirect = RedirectResponse(url="/")
    redirect.delete_cookie("admin_session")
    return redirect

# ==============================================================================
# 📊 3. АДМИН ПАНЕЛЬ И ДАШБОРДЫ
# ==============================================================================

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
    min_p: float = 0.0, max_p: float = 99999.0, supabase: httpx.AsyncClient = Depends(get_supabase_client)
):
    try:
        jwt.decode(request.cookies.get("admin_session", ""), JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401)

    try:
        params = {"price_rub": f"gte.{min_p}", "and": f"(price_rub.lte.{max_p})", "limit": "50"}
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
    try:
        jwt.decode(request.cookies.get("admin_session", ""), JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401)

    if not all([TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_WEBHOOK_SECRET, WEB_APP_URL]):
         return {"error": "Отсутствуют переменные окружения"}

    token_resp = await http_client.post(
        "https://id.twitch.tv/oauth2/token",
        data={"client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET, "grant_type": "client_credentials"}
    )
    if token_resp.status_code != 200: return {"error": "Twitch Auth Failed"}
    
    access_token = token_resp.json()["access_token"]
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    callback_url = f"{WEB_APP_URL}/api/v1/webhooks/twitch"

    subs_resp = await http_client.get("https://api.twitch.tv/helix/eventsub/subscriptions", headers=headers)
    if subs_resp.status_code == 200:
        for sub in subs_resp.json().get("data", []):
            if sub["status"] != "enabled" or callback_url in sub["transport"]["callback"]:
                await http_client.delete(f"https://api.twitch.tv/helix/eventsub/subscriptions?id={sub['id']}", headers=headers)

    event_types = ["channel.channel_points_custom_reward_redemption.add", "stream.online", "stream.offline"]
    created_subs = []
    
    for b_id in ALLOWED_IDS:
        for etype in event_types:
            payload = {
                "type": etype, "version": "1", "condition": {"broadcaster_user_id": b_id},
                "transport": {"method": "webhook", "callback": callback_url, "secret": TWITCH_WEBHOOK_SECRET}
            }
            res = await http_client.post("https://api.twitch.tv/helix/eventsub/subscriptions", headers=headers, json=payload)
            created_subs.append({f"Channel {b_id} - {etype}": res.status_code})

    return {"message": "Успех", "target_webhook": callback_url, "results": created_subs}

# ==============================================================================
# 🔥 5. ВЫДЕЛЕННЫЙ БРОНЕЖИЛЕТ FOSSABOT (МАКСИМАЛЬНАЯ СКОРОСТЬ)
# ==============================================================================

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

        if is_first_blood:
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

class RewardCreateRequest(BaseModel):
    title: str; reward_type: str; steam_item_name: Optional[str] = ""; auto_steam: Optional[bool] = False
    reward_amount: Optional[int] = 10; target_value: Optional[int] = 0; notify_admin: Optional[bool] = True
    show_user_input: Optional[bool] = True

@app.get("/api/v1/admin/rewards")
async def get_admin_rewards_panel(request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    try: jwt.decode(request.cookies.get("admin_session", ""), JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError: raise HTTPException(status_code=401)

    try:
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
                    {"id": u["id"], "login": u["login"], "display_name": u["display_name"], "profile_image": u["profile_image_url"]}
                    for u in tw_res.json().get("data", [])
                ]

        if not channels_metadata:
            channels_metadata = [{"id": b, "login": f"Channel_{b}", "display_name": f"ID: {b}", "profile_image": ""} for b in ALLOWED_IDS]

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

@app.post("/api/v1/admin/rewards/create")
async def create_admin_twitch_reward(req: RewardCreateRequest, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    payload = req.dict(); payload["promocode_amount"] = req.reward_amount
    payload["condition_type"] = "twitch_messages_session" if req.target_value > 0 else "none"
    payload["is_active"] = True
    res = await supabase.post("/rest/v1/twitch_rewards", json=payload)
    if res.status_code in [200, 201, 204]: return {"status": "success"}
    raise HTTPException(status_code=400, detail=res.text)

@app.post("/api/v1/admin/rewards/toggle")
async def toggle_admin_twitch_reward(id: int, status: bool, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    await supabase.patch("/rest/v1/twitch_rewards", params={"id": f"eq.{id}"}, json={"is_active": status})
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

@app.get("/api/v1/admin/boxes/{box_id}/players")
async def get_box_players(box_id: int, search: str = "", request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    try:
        params = {"box_id": f"eq.{box_id}", "order": "id.desc"}
        if search:
            # Умный поиск: ищем и по Twitch логину, и по TG ID
            params["or"] = f"(twitch_login.ilike.*{search.strip()}*,telegram_id.eq.{search.strip() if search.strip().isdigit() else 0})"
        
        res = await supabase.get("/rest/v1/box_players", params=params)
        return res.json() if res.status_code == 200 else []
    except Exception:
        return []

@app.delete("/api/v1/admin/boxes/players/{player_id}")
async def delete_box_player(player_id: int, request: Request, supabase: httpx.AsyncClient = Depends(get_supabase_client)):
    if not request.cookies.get("admin_session"): raise HTTPException(status_code=401)
    try:
        await supabase.delete("/rest/v1/box_players", params={"id": f"eq.{player_id}"})
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
# =========================================================================
# ⚙️ 2. СКРЫТЫЙ ЭНДПОИНТ-ВОРКЕР (Спокойно закупает скин за 10 секунд)
# =========================================================================
class WorkerPayload(BaseModel):
    user_id: int
    target_name: str
    target_price_rub: float
    trade_url: str
    history_id: int

@app.post("/api/v1/internal/worker_buy_skin")
async def worker_buy_skin(payload: WorkerPayload):
    try:
        logging.info(f"⚙️ Воркер начал работу: Закупка {payload.target_name} для юзера {payload.user_id}...")
        
        # ЭТО ЗАГЛУШКА, Т.К. get_background_client и fulfill_item_delivery не импортированы в этом файле. 
        # Скорее всего они у тебя в другом воркере, поэтому оставляем как было, чтобы не ломать логику.
        db = await get_background_client() 
        
        await fulfill_item_delivery(
            user_id=payload.user_id,
            target_name=payload.target_name,
            target_price_rub=payload.target_price_rub,
            trade_url=payload.trade_url,
            supabase=db,
            history_id=payload.history_id,
            source="shop" 
        )
        logging.info(f"✅ Воркер успешно отработал ордер #{payload.history_id}")
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"❌ Воркер сломался на ордере #{payload.history_id}: {e}")
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
