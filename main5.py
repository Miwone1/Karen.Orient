import os
import json
import logging
import asyncio
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from google import genai
from google.genai import types

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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
POLAR_ACCESS_TOKEN = os.environ.get("POLAR_ACCESS_TOKEN")

FILE_NAME = "ski_coach_memory.json"
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
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=SCOPES
        )
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"❌ Ошибка авторизации Google Drive API: {e}")
        return None

def find_memory_file(service):
    """Поиск файла по имени на Google Диске."""
    try:
        query = f"name='{FILE_NAME}' and trashed=false"
        results = service.files().list(
            q=query, 
            fields="files(id, name, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        files = results.get('files', [])
        return files[0]['id'] if files else None
    except Exception as e:
        print(f"❌ Ошибка при поиске файла на Диске: {e}")
        return None

def load_memory_from_drive():
    try:
        service = get_drive_service()
        if not service:
            return []

        file_id = find_memory_file(service)
        if not file_id:
            print(f"ℹ️ Файл {FILE_NAME} не найден на Диске.")
            return []

        request = service.files().get_media(fileId=file_id)
        file_content = request.execute()
        
        content_str = file_content.decode('utf-8').strip()
        if not content_str or content_str in ["{}", "[]"]:
            return []

        history = json.loads(content_str)
        print(f"✅ Память загружена с Google Диска (сообщений в истории: {len(history)})")
        return history if isinstance(history, list) else []
    except Exception as e:
        print(f"❌ Ошибка чтения памяти с Google Диска: {e}")
        return []

def save_memory_to_drive(history):
    """Сохраняет всю накопившуюся историю поверх файла на Google Диске."""
    try:
        service = get_drive_service()
        if not service:
            return

        file_id = find_memory_file(service)

        data_bytes = json.dumps(history, ensure_ascii=False, indent=2).encode('utf-8')
        media = MediaInMemoryUpload(data_bytes, mimetype='application/json', resumable=False)

        if file_id:
            service.files().update(
                fileId=file_id, 
                media_body=media,
                supportsAllDrives=True
            ).execute()
            print(f"✅ [Google Drive] Память успешно сохранена (записей: {len(history)})")
        else:
            print(f"⚠️ Файл {FILE_NAME} не найден на Диске.")
    except Exception as e:
        print(f"❌ Ошибка сохранения памяти на Google Диск: {e}")

# Global memory variable
user_memory = load_memory_from_drive()

# ================================
# POLAR ACCESSLINK INTEGRATION
# ================================
def fetch_polar_data(endpoint):
    """Универсальная функция для запросов к Polar AccessLink API."""
    if not POLAR_ACCESS_TOKEN:
        print("⚠️ POLAR_ACCESS_TOKEN не настроен.")
        return None

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {POLAR_ACCESS_TOKEN}"
    }
    url = f"https://www.polaraccesslink.com/v3/{endpoint}"

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"⚠️ Polar API ({endpoint}) ответил с кодом {response.status_code}: {response.text}")
            return None
    except Exception as e:
        print(f"❌ Ошибка при запросе к Polar AccessLink API ({endpoint}): {e}")
        return None

def fetch_polar_exercises():
    return fetch_polar_data("exercises")

def fetch_polar_sleep():
    return fetch_polar_data("users/sleep")

def fetch_polar_activity():
    return fetch_polar_data("users/activity-transactions")

def fetch_polar_recharge():
    return fetch_polar_data("users/nightly-recharge")

# ================================
# TELEGRAM & GEMINI BOT LOGIC
# ================================
system_instruction = (
    "Ты — персональный AI-тренер по лыжному ориентированию, лыжным гонкам и трейлраннингу. "
    "Ты общаешься со спортсменом высокого уровня (КМС/Сборная). Будь профессиональным, "
    "поддерживающим, анализируй нагрузки, пульсовые зоны, подготовку инвентаря (парафины, Fischer Speedmax, Bliz) "
    "и соревновательную тактику. У тебя есть доступ к истории тренировок, сна, активности, восстановления и диалогов."
)

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я твой AI-тренер по лыжным гонкам и ориентированию. Отправляй сообщения или используй /sync для загрузки данных Polar.")

async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Синхронизация тренировок, сна, активности и восстановления из Polar Flow."""
    await update.message.reply_text("⏳ Запрашиваю данные о тренировках, сне, активности и восстановлении из Polar Flow...")
    
    exercises = fetch_polar_exercises()
    sleep_data = fetch_polar_sleep()
    activity_data = fetch_polar_activity()
    recharge_data = fetch_polar_recharge()

    if not any([exercises, sleep_data, activity_data, recharge_data]):
        await update.message.reply_text("❌ Новые данные в Polar не найдены или возникла ошибка доступа.")
        return

    combined_polar_data = {
        "exercises": exercises,
        "sleep": sleep_data,
        "activity": activity_data,
        "nightly_recharge": recharge_data
    }

    summary_data = json.dumps(combined_polar_data, ensure_ascii=False, indent=2)
    user_memory.append({
        "role": "user", 
        "parts": [{"text": f"[Системные данные Polar] Загружен комплексный отчет (тренировки, сон, активность, восстановление):\n{summary_data}"}]
    })
    
    user_memory.append({
        "role": "model", 
        "parts": [{"text": "Данные тренировок, сна, активности и восстановления успешно сохранены в память."}]
    })

    save_memory_to_drive(user_memory)

    report_details = []
    if exercises: report_details.append("тренировки")
    if sleep_data: report_details.append("сон")
    if activity_data: report_details.append("активность")
    if recharge_data: report_details.append("восстановление")

    synced_types = ", ".join(report_details)
    await update.message.reply_text(
        f"✅ Синхронизация завершена!\nУспешно получены данные: {synced_types}.\n\n"
        f"Данные сохранены в общую память. Задай мне любой вопрос или попроси сделать разбор состояния и тренировок."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_memory
    user_text = update.message.text

    if not ai_client:
        await update.message.reply_text("Ошибка: GEMINI_API_KEY не задан.")
        return

    # 1. Записываем новый вопрос в глобальную память (для сохранения на Диск)
    user_memory.append({"role": "user", "parts": [{"text": user_text}]})

    # Берем последние 30 записей для API, чтобы не превышать квоту 250к токенов в минуту
    recent_context = user_memory[-15:]

    # Автоматические повторные попытки (retry) при лимитах
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = ai_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=recent_context,
                config=types.GenerateContentConfig(system_instruction=system_instruction)
            )
            bot_reply = response.text

            # 2. Записываем ответ AI
            user_memory.append({"role": "model", "parts": [{"text": bot_reply}]})
            
            # 3. Синхронизируем весь массив на Google Диск
            save_memory_to_drive(user_memory)

            await update.message.reply_text(bot_reply)
            break

        except Exception as e:
            error_str = str(e)
            print(f"❌ Ошибка вызова Gemini API (попытка {attempt + 1}): {error_str}")
            
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                if attempt < max_retries - 1:
                    await asyncio.sleep(10 * (attempt + 1))  # Пауза перед повторной попыткой
                    continue
                else:
                    await update.message.reply_text("⏳ Превышен лимит токенов Gemini в минуту. Подожди 1 минуту и отправь сообщение еще раз!")
            else:
                await update.message.reply_text("Произошла ошибка при обработке запроса.")
                break

# ================================
# MAIN ENTRY POINT
# ================================
if __name__ == "__main__":
    Thread(target=run_health_check_server, daemon=True).start()

    print("⏳ Загрузка памяти с Google Диска...")
    user_memory = load_memory_from_drive()

    if not TELEGRAM_BOT_TOKEN:
        print("❌ Ошибка: TELEGRAM_BOT_TOKEN не задан!")
    else:
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("sync", sync_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        print("🚀 Бот-тренер успешно запущен в режиме 24/7!")
        app.run_polling(drop_pending_updates=True)
