import os
import json
import logging
import asyncio
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

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
SCOPES = ['https://www.googleapis.com/auth/drive.file']

# ================================
# HEALTH CHECK SERVER (FOR RENDER)
# ================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("Бот-тренер работает в 24/7 режиме!".encode('utf-8'))

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

def load_memory_from_drive():
    try:
        service = get_drive_service()
        if not service or not DRIVE_FOLDER_ID:
            print("⚠️ DRIVE_FOLDER_ID не задан. Бот работает без постоянной памяти.")
            return []

        # Ищем файл в указанной папке
        query = f"'{DRIVE_FOLDER_ID}' in parents and name='{FILE_NAME}' and trashed=false"
        results = service.files().list(
            q=query, 
            fields="files(id)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        files = results.get('files', [])

        if not files:
            print(f"ℹ️ Файл {FILE_NAME} пока не найден в папке. Проверь имя файла и доступ.")
            return []

        file_id = files[0]['id']
        request = service.files().get_media(fileId=file_id)
        file_content = request.execute()
        
        content_str = file_content.decode('utf-8').strip()
        if not content_str or content_str in ["{}", "[]"]:
            return []

        history = json.loads(content_str)
        print(f"✅ Память загружена с Google Диска (сообщений: {len(history)})")
        return history if isinstance(history, list) else []
    except Exception as e:
        print(f"❌ Ошибка чтения памяти с Google Диска: {e}")
        return []

def save_memory_to_drive(history):
    """Сохраняет массив сообщений поверх существующего файла."""
    try:
        service = get_drive_service()
        if not service or not DRIVE_FOLDER_ID:
            return

        # Ищем файл, созданный пользователем
        query = f"'{DRIVE_FOLDER_ID}' in parents and name='{FILE_NAME}' and trashed=false"
        results = service.files().list(
            q=query, 
            fields="files(id)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        files = results.get('files', [])

        data_bytes = json.dumps(history, ensure_ascii=False, indent=2).encode('utf-8')
        media = MediaInMemoryUpload(data_bytes, mimetype='application/json', resumable=False)

        if files:
            file_id = files[0]['id']
            # Обновляем содержимое файла
            service.files().update(
                fileId=file_id, 
                media_body=media,
                supportsAllDrives=True
            ).execute()
            print(f"✅ [Google Drive] Память записана в файл ID: {file_id}")
        else:
            print(f"⚠️ Файл {FILE_NAME} не найден в папке {DRIVE_FOLDER_ID}. Некуда записывать.")
    except Exception as e:
        print(f"❌ Ошибка сохранения памяти на Google Диск: {e}")

# Global memory variable
user_memory = load_memory_from_drive()

# ================================
# POLAR ACCESSLINK INTEGRATION
# ================================
def fetch_polar_exercises():
    """Получает последние данные о тренировках через Polar AccessLink API."""
    if not POLAR_ACCESS_TOKEN:
        print("⚠️ POLAR_ACCESS_TOKEN не настроен.")
        return None

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {POLAR_ACCESS_TOKEN}"
    }
    url = "https://www.polaraccesslink.com/v3/exercises"

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"⚠️ Polar API ответил с кодом {response.status_code}: {response.text}")
            return None
    except Exception as e:
        print(f"❌ Ошибка при запросе к Polar AccessLink API: {e}")
        return None

# ================================
# TELEGRAM & GEMINI BOT LOGIC
# ================================
system_instruction = (
    "Ты — персональный AI-тренер по лыжному ориентированию, лыжным гонкам и трейлраннингу. "
    "Ты общаешься со спортсменом высокого уровня (КМС/Сборная). Будь профессиональным, "
    "поддерживающим, анализируй нагрузки, пульсовые зоны, подготовку инвентаря (парафины, Fischer Speedmax, Bliz) "
    "и соревновательную тактику."
)

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я твой AI-тренер по лыжным гонкам и ориентированию. Напиши мне или используй команду /sync для получения данных из Polar.")

async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /sync для загрузки тренировок Polar."""
    await update.message.reply_text("⏳ Запрашиваю последние тренировки из Polar Flow...")
    exercises = fetch_polar_exercises()
    
    if not exercises:
        await update.message.reply_text("❌ Не удалось получить новые тренировки из Polar (проверьте POLAR_ACCESS_TOKEN или новые тренировки отсутствуют).")
        return

    summary = f"Данные с Polar Flow: получено тренировок: {len(exercises)}.\n" + json.dumps(exercises, ensure_ascii=False, indent=2)
    
    user_memory.append({"role": "user", "parts": [{"text": f"[Системное сообщение] Проанализируй свежие данные тренировки: {summary}"}]})
    
    # Сохраняем в фоне
    save_memory_to_drive(user_memory)

    recent_history = user_memory[-30:]

    try:
        response = ai_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=recent_history,
            config=types.GenerateContentConfig(system_instruction=system_instruction)
        )
        bot_reply = response.text
        
        user_memory.append({"role": "model", "parts": [{"text": bot_reply}]})
        save_memory_to_drive(user_memory)

        await update.message.reply_text(bot_reply)
    except Exception as e:
        print(f"❌ Ошибка вызова Gemini API при синхронизации Polar: {e}")
        await update.message.reply_text("Произошла ошибка при анализе тренировки.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_memory
    user_text = update.message.text

    if not ai_client:
        await update.message.reply_text("Ошибка: GEMINI_API_KEY не задан.")
        return

    # Добавляем новое сообщение
    user_memory.append({"role": "user", "parts": [{"text": user_text}]})
    
    # Сразу пробуем записать на диск
    save_memory_to_drive(user_memory)

    recent_history = user_memory[-30:]

    try:
        response = ai_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=recent_history,
            config=types.GenerateContentConfig(system_instruction=system_instruction)
        )
        bot_reply = response.text

        user_memory.append({"role": "model", "parts": [{"text": bot_reply}]})
        save_memory_to_drive(user_memory)

        await update.message.reply_text(bot_reply)

    except Exception as e:
        print(f"❌ Ошибка вызова Gemini API: {e}")
        await update.message.reply_text("Произошла ошибка при обработке запроса к AI. Попробуй позже.")

# ================================
# MAIN ENTRY POINT
# ================================
if __name__ == "__main__":
    Thread(target=run_health_check_server, daemon=True).start()

    print("⏳ Проверка прав доступа к Google Диску...")
    save_memory_to_drive(user_memory)

    if not TELEGRAM_BOT_TOKEN:
        print("❌ Ошибка: TELEGRAM_BOT_TOKEN не задан!")
    else:
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("sync", sync_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        print("🚀 Бот-тренер успешно запущен в режиме 24/7!")
        app.run_polling(drop_pending_updates=True)
        
