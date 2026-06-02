import os
import logging
import difflib
import re
from datetime import date, timedelta
from dotenv import load_dotenv

import assemblyai as aai
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TODOIST_TOKEN = os.environ["TODOIST_TOKEN"]
TODOIST_PROJECT_ID = os.environ["TODOIST_PROJECT_ID"]
ASSEMBLYAI_API_KEY = os.environ["ASSEMBLYAI_API_KEY"]

aai.settings.api_key = ASSEMBLYAI_API_KEY

TODOIST_BASE = "https://api.todoist.com/api/v1"
TODOIST_HEADERS = {"Authorization": f"Bearer {TODOIST_TOKEN}"}

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def parse_date(text: str) -> tuple[str | None, str | None]:
    """Return (todoist_date_str, human_label) or (None, None)."""
    text_lower = text.lower()
    today = date.today()

    if "послезавтра" in text_lower:
        d = today + timedelta(days=2)
        return d.isoformat(), "послезавтра"
    if "завтра" in text_lower:
        d = today + timedelta(days=1)
        return d.isoformat(), "завтра"
    if "сегодня" in text_lower:
        return today.isoformat(), "сегодня"

    def _try(day: int, month: int, year: int, label: str) -> tuple[str | None, str | None]:
        try:
            return date(year, month, day).isoformat(), label
        except ValueError:
            return None, None

    # DDMMYYYY — 8 digits, e.g. 26032026
    m = re.search(r"\b(\d{2})(\d{2})(\d{4})\b", text)
    if m:
        iso, lbl = _try(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
        if iso:
            return iso, lbl

    # DD.MM.YYYY
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
    if m:
        iso, lbl = _try(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
        if iso:
            return iso, lbl

    # DD.MM.YY
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2})\b", text)
    if m:
        year = 2000 + int(m.group(3))
        iso, lbl = _try(int(m.group(1)), int(m.group(2)), year,
                        f"{m.group(1)}.{m.group(2)}.{year}")
        if iso:
            return iso, lbl

    # DD MM YYYY — space-separated numbers, e.g. 26 03 2026
    m = re.search(r"\b(\d{1,2})\s+(\d{1,2})\s+(\d{4})\b", text)
    if m:
        iso, lbl = _try(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
        if iso:
            return iso, lbl

    # DDMMYY — 6 digits, e.g. 260326
    m = re.search(r"\b(\d{2})(\d{2})(\d{2})\b", text)
    if m:
        year = 2000 + int(m.group(3))
        iso, lbl = _try(int(m.group(1)), int(m.group(2)), year,
                        f"{m.group(1)}.{m.group(2)}.{year}")
        if iso:
            return iso, lbl

    # "5 июня 2026" or "на 5 июня 2026" — Russian month with explicit year
    m = re.search(r"(?:на\s+)?(\d{1,2})\s+([а-яё]+)\s+(\d{4})", text_lower)
    if m:
        day, month_word, year = int(m.group(1)), m.group(2), int(m.group(3))
        mo = MONTHS_RU.get(month_word)
        if mo:
            iso, _ = _try(day, mo, year, "")
            if iso:
                return iso, f"{day} {month_word} {year}"

    # "5 июня" or "на 26 марта" — Russian month, auto year
    m = re.search(r"(?:на\s+)?(\d{1,2})\s+([а-яё]+)", text_lower)
    if m:
        day, month_word = int(m.group(1)), m.group(2)
        mo = MONTHS_RU.get(month_word)
        if mo:
            for year in (today.year, today.year + 1):
                iso, _ = _try(day, mo, year, "")
                if iso and date.fromisoformat(iso) >= today:
                    return iso, f"{day} {month_word}"

    return None, None


def strip_date_phrase(text: str) -> str:
    """Remove date-related words from task name."""
    text = re.sub(r"послезавтра", "", text, flags=re.IGNORECASE)
    text = re.sub(r"завтра", "", text, flags=re.IGNORECASE)
    text = re.sub(r"сегодня", "", text, flags=re.IGNORECASE)
    # Numeric formats — longest patterns first to avoid partial matches
    text = re.sub(r"\b\d{2}\d{2}\d{4}\b", "", text)                           # DDMMYYYY
    text = re.sub(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b", "", text)                   # DD.MM.YYYY
    text = re.sub(r"\b\d{1,2}\.\d{1,2}\.\d{2}\b", "", text)                   # DD.MM.YY
    text = re.sub(r"\b\d{1,2}\s+\d{1,2}\s+\d{4}\b", "", text)                 # DD MM YYYY
    text = re.sub(r"\b\d{2}\d{2}\d{2}\b", "", text)                            # DDMMYY
    # Russian month names with optional "на" and optional year
    text = re.sub(r"(?:на\s+)?\d{1,2}\s+[а-яё]+(?:\s+\d{4})?", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", text).strip()


def extract_intent(text: str) -> str:
    """complete | reschedule | create_with_date | create"""
    lower = text.lower()

    done_keywords = ["выполнено", "выполнена", "готово", "сделано"]
    if any(k in lower for k in done_keywords):
        return "complete"

    reschedule_keywords = ["перенеси", "перенести", "сдвинь", "сдвинуть"]
    if any(k in lower for k in reschedule_keywords):
        return "reschedule"

    create_keywords = ["поставь", "поставить", "создай", "создать", "добавь", "добавить"]
    date_str, _ = parse_date(text)
    if any(k in lower for k in create_keywords) and date_str:
        return "create_with_date"

    if date_str:
        return "create_with_date"

    return "create"


def clean_task_name(text: str, intent: str) -> str:
    """Strip intent-related keywords and date phrases to get the task name."""
    lower_map = {
        "complete": [
            "задача", "выполнено", "выполнена", "готово", "сделано",
        ],
        "reschedule": [
            "перенеси", "перенести", "сдвинь", "сдвинуть", "задачу", "задача",
        ],
        "create_with_date": [
            "поставь", "поставить", "создай", "создать", "добавь", "добавить",
            "задачу", "задача", "поставить задачу", "создать задачу",
        ],
        "create": [
            "поставь", "поставить", "создай", "создать", "добавь", "добавить",
            "задачу", "задача", "поставить задачу", "создать задачу",
        ],
    }
    result = text
    for kw in lower_map.get(intent, []):
        result = re.sub(rf"\b{re.escape(kw)}\b", "", result, flags=re.IGNORECASE)
    result = strip_date_phrase(result)
    # clean up extra spaces and leading/trailing punctuation
    result = re.sub(r"\s{2,}", " ", result).strip(" ,.-:")
    return result


# ── Todoist helpers ───────────────────────────────────────────────────────────

def todoist_get_tasks() -> list[dict]:
    resp = requests.get(
        f"{TODOIST_BASE}/tasks",
        headers=TODOIST_HEADERS,
        params={"project_id": TODOIST_PROJECT_ID},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", data)


def todoist_create_task(name: str, due_date: str | None = None) -> dict:
    payload: dict = {"content": name, "project_id": TODOIST_PROJECT_ID}
    if due_date:
        payload["due_date"] = due_date
    resp = requests.post(
        f"{TODOIST_BASE}/tasks",
        headers=TODOIST_HEADERS,
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def todoist_complete_task(task_id: str) -> None:
    resp = requests.post(
        f"{TODOIST_BASE}/tasks/{task_id}/close",
        headers=TODOIST_HEADERS,
        timeout=10,
    )
    resp.raise_for_status()


def todoist_update_task(task_id: str, due_date: str) -> None:
    resp = requests.post(
        f"{TODOIST_BASE}/tasks/{task_id}",
        headers=TODOIST_HEADERS,
        json={"due_date": due_date},
        timeout=10,
    )
    resp.raise_for_status()


def find_task(name_query: str, tasks: list[dict]) -> dict | None:
    if not tasks:
        return None
    names = [t["content"] for t in tasks]
    matches = difflib.get_close_matches(name_query, names, n=1, cutoff=0.4)
    if matches:
        match_name = matches[0]
        return next(t for t in tasks if t["content"] == match_name)
    # fallback: substring match
    query_lower = name_query.lower()
    for task in tasks:
        if query_lower in task["content"].lower():
            return task
    return None


# ── Transcription ─────────────────────────────────────────────────────────────

def transcribe(file_path: str) -> str:
    transcriber = aai.Transcriber()
    config = aai.TranscriptionConfig(language_code="ru")
    transcript = transcriber.transcribe(file_path, config=config)
    if transcript.status == aai.TranscriptStatus.error:
        logger.error("AssemblyAI error: %s", transcript.error)
        return ""
    return transcript.text or ""


# ── Handler ───────────────────────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)

    local_path = f"/tmp/{voice.file_id}.ogg"
    await tg_file.download_to_drive(local_path)

    text = transcribe(local_path)
    try:
        os.remove(local_path)
    except OSError:
        pass

    if not text:
        await update.message.reply_text("❌ Не удалось распознать голосовое")
        return

    logger.info("Transcribed: %s", text)

    intent = extract_intent(text)
    task_name = clean_task_name(text, intent)

    if not task_name:
        await update.message.reply_text("❌ Не удалось определить название задачи")
        return

    try:
        if intent == "create":
            todoist_create_task(task_name)
            await update.message.reply_text(f"✅ Задача создана: {task_name}")

        elif intent == "create_with_date":
            date_str, label = parse_date(text)
            todoist_create_task(task_name, date_str)
            await update.message.reply_text(
                f"✅ Задача создана: {task_name} — {label}"
            )

        elif intent == "complete":
            tasks = todoist_get_tasks()
            task = find_task(task_name, tasks)
            if not task:
                await update.message.reply_text(f"❌ Задача не найдена: {task_name}")
                return
            todoist_complete_task(task["id"])
            await update.message.reply_text(f"✅ Задача выполнена: {task['content']}")

        elif intent == "reschedule":
            date_str, label = parse_date(text)
            if not date_str:
                await update.message.reply_text("❌ Не удалось распознать дату для переноса")
                return
            tasks = todoist_get_tasks()
            task = find_task(task_name, tasks)
            if not task:
                await update.message.reply_text(f"❌ Задача не найдена: {task_name}")
                return
            todoist_update_task(task["id"], date_str)
            await update.message.reply_text(
                f"✅ Задача перенесена: {task['content']} → {label}"
            )

    except requests.HTTPError as exc:
        logger.error("Todoist API error: %s", exc)
        await update.message.reply_text("❌ Ошибка при работе с Todoist")


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    logger.info("Bot started (polling)")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
