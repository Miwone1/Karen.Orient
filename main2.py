import io
import json
import os
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from google import genai
from google.genai import types
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# --- ПОЛУЧЕНИЕ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ИЗ НАСТРОЕК СЕРВЕРА ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
POLAR_ACCESS_TOKEN = os.getenv("POLAR_ACCESS_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

FILE_NAME = "ski_coach_memory.json"

# --- ВЕБ-СЕРВЕР ПРОВЕРКИ ЗДОРОВЬЯ ДЛЯ БЕСПЛАТНОГО WEB SERVICE НА RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return  # Отключаем лишний спам-лог запросов сервера в консоли

def run_health_check_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# --- РАБОТА С GOOGLE DRIVE API ---
def get_drive_service():
    if not GOOGLE_CREDENTIALS_JSON:
        print("⚠️ Переменная GOOGLE_CREDENTIALS не задана!")
        return None
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds)

def load_memory_from_drive():
    try:
        service = get_drive_service()
        if not service or not DRIVE_FOLDER_ID:
            return []

        query = f"'{DRIVE_FOLDER_ID}' in parents and name='{FILE_NAME}' and trashed=false"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get('files', [])

        if not files:
            return []

        file_id = files[0]['id']
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        fh.seek(0)
        return json.loads(fh.read().decode('utf-8'))
    except Exception as e:
        print(f"Ошибка загрузки памяти с Google Диска: {e}")
        return []

def save_memory_to_drive(history):
    try:
        service = get_drive_service()
        if not service or not DRIVE_FOLDER_ID:
            return

        query = f"'{DRIVE_FOLDER_ID}' in parents and name='{FILE_NAME}' and trashed=false"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get('files', [])

        # Сохраняем ВСЮ историю полностью без любых срезов и ограничений
        data_str = json.dumps(history, ensure_ascii=False, indent=2)
        media = MediaInMemoryUpload(data_str.encode('utf-8'), mimetype='application/json')

        if files:
            file_id = files[0]['id']
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            file_metadata = {'name': FILE_NAME, 'parents': [DRIVE_FOLDER_ID]}
            service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        print("✅ Память успешно синхронизирована с Google Диском!")
    except Exception as e:
        print(f"Ошибка сохранения памяти на Google Диск: {e}")

# --- ИИ-ТРЕНЕР И POLAR FLOW ---
SYSTEM_INSTRUCTION = """
Ты — личный элитный тренер по лыжному ориентированию. Твой спортсмен — КМС (2011 г.р.), кандидат в юниорскую сборную России.
Учитывай специфику вида спорта:
- Высокая физическая нагрузка (работа одновременным бесшажным и попеременным ходами на сетке лыжней различной ширины).
- Динамика пульсовых зон, контроль закисления, восстановление плечевого пояса и ног.
- Ментальная свежесть для чтения карты и принятия решений по выбору варианта на высокой ЧСС (180+).
- Твой тон: профессиональный, требовательный, мотивирующий, но с фокусом на здоровье и долгосрочный прогресс атлета.
"""

def get_polar_data():
    if POLAR_ACCESS_TOKEN:
        try:
            headers = {"Authorization": f"Bearer {POLAR_ACCESS_TOKEN}", "Accept": "application/json"}
            
            # 1. Данные о тренировках
            exercises = requests.get("https://www.polaraccesslink.com/v3/exercises", headers=headers)
            
            # 2. Фазы и продолжительность сна
            sleep = requests.get("https://www.polaraccesslink.com/v3/users/nights", headers=headers)
            
            # 3. Восстановление (Nightly Recharge / HRV)
            recharge = requests.get("https://www.polaraccesslink.com/v3/users/nightly-recharge", headers=headers)

            return {
                "exercises": exercises.json() if exercises.status_code == 200 else [],
                "sleep": sleep.json() if sleep.status_code == 200 else {},
                "recharge": recharge.json() if recharge.status_code == 200 else {}
            }
        except Exception as e:
            print(f"Ошибка получения данных Polar: {e}")
    return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if TELEGRAM_CHAT_ID and str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return

    user_text = update.message.text
    
    # 1. Загрузка ВСЕЙ истории с Google Диска
    history = load_memory_from_drive()

    # 2. Запрос данных Polar Flow при наличии ключевых слов
    polar_context = ""
    if any(w in user_text.lower() for w in ["тренировк", "polar", "пульс", "состояни", "анализ", "разбор", "самочувстви"]):
        data = get_polar_data()
        if data:
            polar_context = f"\n[Свежие данные Polar Flow: {json.dumps(data, ensure_ascii=False)}]\n"

    # 3. Превращение ВСЕЙ истории переписки в форматированный диалоговый текст
    formatted_history = ""
    for turn in history:
        formatted_history += f"Спортсмен: {turn.get('user', '')}\nТренер: {turn.get('coach', '')}\n\n"

    full_prompt = f"Предыдущий диалог:\n{formatted_history}\n{polar_context}\nСпортсмен: {user_text}"

    # 4. Отправка запроса в Gemini с сохранением вашей модели и системной инструкции
    ai_client = genai.Client(api_key=GEMINI_API_KEY)
    response = ai_client.models.generate_content(
        model="gemini-3.6-flash",
        contents=full_prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.7
        )
    )

    answer = response.text

    # 5. Добавление нового ответа и полное сохранение ВСЕЙ истории на Google Диск
    history.append({"user": user_text, "coach": answer})
    save_memory_to_drive(history)

    await update.message.reply_text(answer)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я твой ИИ-тренер по лыжному ориентированию (работает 24/7 в облаке). Задавай вопросы или запрашивай разбор Polar!")

if __name__ == "__main__":
    # 1. Запуск веб-сервера для Render
    threading.Thread(target=run_health_check_server, daemon=True).start()
    print("🚀 Фоновый веб-сервер проверки состояния запущен.")

    # 2. Запуск Telegram-бота
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 Бот-тренер успешно запущен в режиме 24/7!")
    app.run_polling()
