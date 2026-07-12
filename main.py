import os
import httpx
from fastapi import FastAPI, HTTPException, Depends

app = FastAPI(title="Stream Panel API")

# Простейший хелпер для получения клиента (замена твоей функции для теста)
async def get_test_supabase_client():
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") # или тот ключ, что используется на бэке
    
    if not supabase_url or not supabase_key:
        raise HTTPException(status_code=500, detail="Ключи Supabase не найдены в Environment Variables!")
        
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}"
    }
    async with httpx.AsyncClient(base_url=supabase_url, headers=headers) as client:
        yield client

@app.get("/")
async def root():
    return {
        "status": "ok", 
        "message": "Стримовский инстанс успешно запущен и готов к работе!"
    }

# Тестовый роут для проверки соединения с Supabase
@app.get("/api/v1/test-db")
async def test_db_connection(supabase: httpx.AsyncClient = Depends(get_test_supabase_client)):
    try:
        # Делаем пробный быстрый запрос к таблице settings, как в твоем кроне
        response = await supabase.get("/rest/v1/settings", params={"limit": 1})
        if response.status_code == 200:
            return {
                "status": "success",
                "message": "Связь с Supabase успешно установлена!",
                "data_preview": response.json()
            }
        else:
            return {
                "status": "error",
                "code": response.status_code,
                "error_details": response.text
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка при подключении к базе: {str(e)}")
