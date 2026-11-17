import os
import json
import logging
import asyncio
import requests
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from datetime import datetime

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from openai import OpenAI

# ================================
# LOGGING SETUP
# ================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ================================
# CONFIG & ENVs
# ================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
POLAR_ACCESS_TOKEN = os.environ.get("POLAR_ACCESS_TOKEN")

TRAINING_FILE_NAME = "ski_coach_data.json"
CHAT_FILE_NAME = "ski_coach_chat.json"
SCOPES = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/drive']

# ================================
# HEALTH CHECK SERVER (FOR RENDER)
# ================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("Бот-тренер работает в 24/7 режиме!".encode('utf-8'))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server_address = ('', port)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    print(f"🚀 Фоновый веб-сервер проверки состояния запущен на порту {port}")
    httpd.serve_forever()

# ================================
# GOOGLE DRIVE INTEGRATION
# ================================
def get_drive_service():
    if not GOOGLE_CREDENTIALS_JSON:
        print("⚠️ Переменная GOOGLE_CREDENTIALS_JSON не найдена.")
        return None
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"❌ Ошибка авторизации Google Drive API: {e}")
        return None

def find_file(service, filename):
    try:
        query = f"name='{filename}' and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)", supportsAllDrives=True).execute()
        files = results.get('files', [])
        return files[0]['id'] if files else None
    except Exception as e:
        print(f"❌ Ошибка поиска файла {filename}: {e}")
        return None

def load_json_from_drive(filename):
    try:
        service = get_drive_service()
        if not service: return {}
        file_id = find_file(service, filename)
        if not file_id: return {}
        
        request = service.files().get_media(fileId=file_id)
        content = request.execute().decode('utf-8').strip()
        return json.loads(content) if content else {}
    except Exception as e:
        print(f"❌ Ошибка чтения {filename}: {e}")
        return {}

def save_json_to_drive(filename, data):
    try:
        service = get_drive_service()
        if not service: return
        file_id = find_file(service, filename)
        data_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        media = MediaInMemoryUpload(data_bytes, mimetype='application/json', resumable=False)
        
        if file_id:
            service.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
            print(f"✅ [Google Drive] {filename} обновлен.")
        else:
            file_metadata = {'name': filename}
            if DRIVE_FOLDER_ID: file_metadata['parents'] = [DRIVE_FOLDER_ID]
            service.files().create(body=file_metadata, media_body=media, fields='id').execute()
            print(f"✅ [Google Drive] {filename} создан.")
    except Exception as e:
        print(f"❌ Ошибка сохранения {filename}: {e}")

# Глобальные переменные
training_database = {} 
chat_history = []      

# ================================
# POLAR ACCESSLINK INTEGRATION
# ================================
def fetch_polar_data(endpoint):
    if not POLAR_ACCESS_TOKEN: return None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {POLAR_ACCESS_TOKEN}"}
    url = f"https://www.polaraccesslink.com/v3/{endpoint}"
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200: return response.json()
        print(f"⚠️ Polar API ({endpoint}) ответил с кодом {response.status_code}: {response.text}")
        return None
    except Exception as e:
        print(f"❌ Ошибка Polar API ({endpoint}): {e}")
        return None

def fetch_polar_exercises(): return fetch_polar_data("exercises")
def fetch_polar_sleep(): return fetch_polar_data("users/sleep")
def fetch_polar_activity(): return fetch_polar_data("users/activity-transactions")
def fetch_polar_recharge(): return fetch_polar_data("users/nightly-recharge")

# ================================
# TELEGRAM & DEEPSEEK LOGIC
# ================================
# Указываем текущую дату и правила, чтобы модель не выдумывала годы
current_date = datetime.now().strftime("%Y-%m-%d")
system_instruction = (
    "Ты — персональный AI-тренер по лыжному ориентированию, лыжным гонкам и трейлраннингу. "
    "Ты общаешься со спортсменом высокого уровня (КМС/Сборная). Будь профессиональным, "
    "поддерживающим, анализируй нагрузки, пульсовые зоны, подготовку инвентаря (парафины, Fischer Speedmax, Bliz) "
    "и соревновательную тактику. "
    f"ВАЖНО: Сегодня {current_date}. Если пользователь просит 'последнюю тренировку' или 'отчет за период', "
    "используй функцию get_training_data_by_date с правильными датами. Не выдумывай даты из прошлых лет."
)

ai_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com") if DEEPSEEK_API_KEY else None

# Описание инструментов
tools = [{
    "type": "function",
    "function": {
        "name": "get_training_data_by_date",
        "description": "Получает данные о тренировках, сне и восстановлении за конкретный период дат.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Начальная дата YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "Конечная дата YYYY-MM-DD"}
            },
            "required": ["start_date", "end_date"]
        }
    }
}]

def get_training_data_by_date(start_date: str, end_date: str) -> str:
    filtered_data = {}
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
        
        for date_str, data in training_database.items():
            current_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
            if start_dt <= current_dt <= end_dt:
                filtered_data[date_str] = data
                
        if not filtered_data:
            return f"За период с {start_date} по {end_date} данных в памяти не найдено."
            
        return json.dumps(filtered_data, ensure_ascii=False)
    except Exception as e:
        return f"Ошибка при поиске данных: {str(e)}"

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я твой AI-тренер. Отправляй сообщения или используй /sync для загрузки данных Polar.")

async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global training_database
    await update.message.reply_text("⏳ Запрашиваю данные из Polar Flow (тренировки, сон, активность, восстановление)...")
    
    exercises_raw = fetch_polar_exercises()
    sleep_raw = fetch_polar_sleep()
    activity_raw = fetch_polar_activity()
    recharge_raw = fetch_polar_recharge()

    if not any([exercises_raw, sleep_raw, activity_raw, recharge_raw]):
        await update.message.reply_text("❌ Новые данные в Polar не найдены или ошибка токена.")
        return

    grouped_data = {}

    def get_list(data, key):
        if isinstance(data, list): return data
        if isinstance(data, dict): return data.get(key, [])
        return []

    for ex in get_list(exercises_raw, "exercises"):
        if isinstance(ex, dict):
            date_str = ex.get("start-time", "")[:10] or ex.get("date", "")
            if date_str:
                if date_str not in grouped_data: grouped_data[date_str] = {}
                if "exercises" not in grouped_data[date_str]: grouped_data[date_str]["exercises"] = []
                grouped_data[date_str]["exercises"].append(ex)

    for sl in get_list(sleep_raw, "nights"):
        if isinstance(sl, dict):
            date_str = sl.get("date", "")
            if date_str:
                if date_str not in grouped_data: grouped_data[date_str] = {}
                grouped_data[date_str]["sleep"] = sl
                
    for act in get_list(activity_raw, "activity-transactions"):
        if isinstance(act, dict):
            date_str = act.get("date", "")
            if date_str:
                if date_str not in grouped_data: grouped_data[date_str] = {}
                grouped_data[date_str]["activity"] = act

    for rec in get_list(recharge_raw, "recharges"):
        if isinstance(rec, dict):
            date_str = rec.get("date", "")
            if date_str:
                if date_str not in grouped_data: grouped_data[date_str] = {}
                grouped_data[date_str]["recharge"] = rec

    if grouped_data:
        training_database.update(grouped_data)
        save_json_to_drive(TRAINING_FILE_NAME, training_database)
        await update.message.reply_text(f"✅ Синхронизация завершена! Добавлены данные за {len(grouped_data)} дней. База обновлена.")
    else:
        await update.message.reply_text("⚠️ Данные получены, но не удалось распознать даты. Возможно, структура ответа Polar изменилась.")

# Функция для ручного парсинга галлюцинаций DeepSeek (теги <|DSML|>)
def parse_dsml_text(text):
    if "<|DSML|" not in text:
        return None
    
    # Ищем аргументы
    start_match = re.search(r'name="start_date"[^>]*>([^<]+)', text)
    end_match = re.search(r'name="end_date"[^>]*>([^<]+)', text)
    
    if start_match and end_match:
        return start_match.group(1).strip(), end_match.group(1).strip()
    return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_history
    user_text = update.message.text

    if not ai_client:
        await update.message.reply_text("Ошибка: DEEPSEEK_API_KEY не задан.")
        return

    chat_history.append({"role": "user", "content": user_text})
    recent_context = chat_history[-10:]

    messages = [
        {"role": "system", "content": system_instruction},
        *recent_context
    ]

    try:
        # ИСПОЛЬЗУЕМ deepseek-chat, так как он лучше работает с Function Calling
        response = ai_client.chat.completions.create(
            model="deepseek-chat", 
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )

        response_message = response.choices[0].message
        bot_reply = response_message.content or ""
        
        # 1. Штатная обработка вызова функции
        if response_message.tool_calls:
            function_name = response_message.tool_calls[0].function.name
            if function_name == "get_training_data_by_date":
                args = json.loads(response_message.tool_calls[0].function.arguments)
                function_response = get_training_data_by_date(args["start_date"], args["end_date"])
                
                messages.append(response_message)
                messages.append({
                    "role": "tool",
                    "tool_call_id": response_message.tool_calls[0].id,
                    "name": function_name,
                    "content": function_response
                })
                
                second_response = ai_client.chat.completions.create(
                    model="deepseek-chat",
                    messages=messages
                )
                bot_reply = second_response.choices[0].message.content
        
        # 2. Защита от галлюцинаций (если модель вернула теги в виде текста)
        elif "<|DSML|" in bot_reply:
            print("⚠️ Обнаружены DSML-теги в тексте. Пытаемся распарсить...")
            parsed_dates = parse_dsml_text(bot_reply)
            if parsed_dates:
                start_date, end_date = parsed_dates
                function_response = get_training_data_by_date(start_date, end_date)
                
                # Отправляем данные обратно модели
                messages.append({"role": "assistant", "content": bot_reply})
                messages.append({"role": "user", "content": f"Вот данные, которые ты запросил:\n{function_response}\n\nПожалуйста, проанализируй их и ответь пользователю."})
                
                second_response = ai_client.chat.completions.create(
                    model="deepseek-chat",
                    messages=messages
                )
                bot_reply = second_response.choices[0].message.content
            else:
                bot_reply = "Не удалось распознать даты в запросе. Попробуй сформулировать иначе (например: 'Отчет с 2026-11-01 по 2026-11-15')."

        chat_history.append({"role": "assistant", "content": bot_reply})
        save_json_to_drive(CHAT_FILE_NAME, {"history": chat_history})

        await update.message.reply_text(bot_reply)

    except Exception as e:
        print(f"❌ Ошибка DeepSeek: {e}")
        await update.message.reply_text("Произошла ошибка при обработке запроса. Проверьте логи на Render.")

# ================================
# MAIN ENTRY POINT
# ================================
if __name__ == "__main__":
    Thread(target=run_health_check_server, daemon=True).start()

    print("⏳ Загрузка баз данных с Google Диска...")
    training_database = load_json_from_drive(TRAINING_FILE_NAME)
    chat_data = load_json_from_drive(CHAT_FILE_NAME)
    chat_history = chat_data.get("history", [])

    if not TELEGRAM_BOT_TOKEN or not DEEPSEEK_API_KEY:
        print("❌ Ошибка: Проверьте переменные TELEGRAM_BOT_TOKEN и DEEPSEEK_API_KEY!")
    else:
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("sync", sync_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        print("🚀 Бот-тренер успешно запущен на DeepSeek с базой данных по датам!")
        app.run_polling(drop_pending_updates=True)
