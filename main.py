import io
import json
import os
import requests
import nest_asyncio
from google import genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

nest_asyncio.apply()

# --- ПОЛУЧЕНИЕ ПЕРЕМЕННЫХ ИЗ НАСТРОЕК СЕРВЕРА ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
POLAR_ACCESS_TOKEN = os.getenv("POLAR_ACCESS_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

FILE_NAME = "ski_coach_memory.json"

# --- РАБОТА С GOOGLE DRIVE API ---
def get_drive_service():
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds)

def load_memory_from_drive():
    try:
        service = get_drive_service()
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
        query = f"'{DRIVE_FOLDER_ID}' in parents and name='{FILE_NAME}' and trashed=false"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get('files', [])

        data_str = json.dumps(history[-30:], ensure_ascii=False, indent=2)
        media = MediaInMemoryUpload(data_str.encode('utf-8'), mimetype='application/json')

        if files:
            file_id = files[0]['id']
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            file_metadata = {'name': FILE_NAME, 'parents': [DRIVE_FOLDER_ID]}
            service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        print("✅ Память успешно сохранена на Google Диск!")
    except Exception as e:
        print(f"Ошибка сохранения памяти на Google Диск: {e}")

# --- ИИ-ТРЕНЕР И TELEGRAM ---
SYSTEM_INSTRUCTION = """
Ты — личный элитный тренер по лыжному ориентированию. Твой спортсмен — КМС (2011 г.р.), кандидат в юниорскую сборную России.
Учитывай специфику вида спорта: работу одновременным и попеременным ходами, сетку лыжней, пульсовые зоны, лактат и чтение карты на высокой ЧСС.
"""

ai_client = genai.Client(api_key=GEMINI_API_KEY)

def get_polar_data():
    if POLAR_ACCESS_TOKEN:
        res = requests.get(
            "https://www.polaraccesslink.com/v3/exercises",
            headers={"Authorization": f"Bearer {POLAR_ACCESS_TOKEN}", "Accept": "application/json"}
        )
        if res.status_code == 200:
            return res.json()
    return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if TELEGRAM_CHAT_ID and str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return

    user_text = update.message.text
    history = load_memory_from_drive()

    polar_context = ""
    if any(w in user_text.lower() for w in ["тренировк", "polar", "пульс", "состояни", "анализ", "разбор"]):
        data = get_polar_data()
        if data:
            polar_context = f"\n[Свежие данные Polar Flow: {json.dumps(data, ensure_ascii=False)}]\n"

    full_prompt = f"{SYSTEM_INSTRUCTION}\nИстория:\n{json.dumps(history, ensure_ascii=False)}\n{polar_context}\nСпортсмен: {user_text}"

    response = ai_client.models.generate_content(
        model="gemini-3.6-flash",
        contents=full_prompt
    )

    answer = response.text
    history.append({"user": user_text, "coach": answer})
    save_memory_to_drive(history)

    await update.message.reply_text(answer)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот-тренер работает 24/7! Данные синхронизируются с Google Диском.")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
