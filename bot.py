"""Бот для канала @AlMaNewsToday.

Собирает свежие посты из публичных телеграм-каналов, переписывает их через ИИ
(Gemini -> Groq -> OpenRouter, если один не отвечает, берётся следующий)
и публикует в канал. Запускается по расписанию из GitHub Actions.
"""
import json
import math
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ===================== НАСТРОЙКИ =====================
CHANNEL = "@AlMaNewsToday"
# username без @. Новые источники просто дописывайте в список.
# Если канал не открывается или пуст, бот напишет об этом админу.
SOURCES = [
    "topor", "uranews", "theohdaily", "Dyadyabatya0",
    "mash", "breakingmash", "bazabazon", "readovkanews", "rbc_news", "tass_agency",
    "bloknot_rossiya", "bbbreaking", "pravdadirty",
]
HEADER_EMOJI = "⚡"           # смайлик в начале каждого поста; "" = без него
SIGNATURE = "@AlMaNewsToday"  # подпись в конце поста; "" = без подписи
USE_MEDIA = True              # брать все фото и видео из исходного поста
TEXT_ONLY_SHARE = 0.2         # доля постов без фото/видео среди последних публикаций (0.2 = примерно каждый пятый)
MEDIA_HISTORY = 10            # по скольким последним публикациям считается эта доля
TEXT_ONLY_PENALTY = 1.5       # на сколько баллов посты без медиа проигрывают в очереди
TEXT_ONLY_IDLE_HOURS = 3      # если столько часов ничего не выходило, можно и без медиа
MAX_AGE_DAY_HOURS = 3         # окно свежести днём (запас на задержки cron; повторы режет seen)
MAX_AGE_NIGHT_HOURS = 4       # ночью постов меньше, окно шире
MIN_LENGTH = 20               # слишком короткие посты пропускаем (защита от пустых и обрывков)
DAY_FROM_HOUR = 8             # "день" по Москве: с 08:00 до 23:00
DAY_TO_HOUR = 23
QUEUE_SHARE = 0.15            # какую долю очереди публиковать за один запуск (0.15 = 15%)
MIN_POSTS_PER_RUN = 1         # но не меньше стольки (если очередь не пуста)
MAX_POSTS_PER_RUN = 5         # и не больше стольки, чтобы не заспамить канал
POST_PAUSE_MIN_SEC = 30       # пауза между постами в одном запуске: случайная
POST_PAUSE_MAX_SEC = 90       # от 30 секунд до 1,5 минуты
# --- ручная модерация: бот присылает пост тебе в личку (ADMIN_CHAT_ID) с кнопками ---
MODERATION = True             # режим по умолчанию: True = ручная, False = автопубликация.
                              # На ходу переключается командами /manual и /auto в личке с ботом
MANUAL_QUEUE_SHARE = 0.5      # ручной режим: какую долю очереди присылать на одобрение за запуск (0.5 = 50%)
MANUAL_IDLE_HOURS = 2         # нет твоих нажатий и сообщений столько часов -> бот сам публикует, как в авто (QUEUE_SHARE)
AUTO_RELEASE_PER_RUN = 1      # в авто-режиме: сколько неодобренных постов из очереди модерации выпускать за запуск
MODERATE_AT_NIGHT = True      # False = ночью (23:00-08:00 МСК) посты выходят сами, без одобрения
MAX_PENDING = 8               # сколько постов может одновременно ждать твоего решения
PENDING_TTL_HOURS = 6         # неодобренный пост через столько часов считается устаревшим и снимается
DAILY_LIMIT = 80              # максимум постов в сутки
RECENT_FOR_AI = 15            # сколько последних постов показывать ИИ для проверки повторов
MIN_SCORE = 5                 # новости с оценкой ИИ ниже этого (шкала 1-10) не публикуем
POOL_SIZE = 20                # сколько свежих постов оценивать за раз
DUP_THRESHOLD = 0.6           # доля общих слов, после которой пост считается повтором
MAX_PUBLISH_FAILS = 3         # после стольких неудачных публикаций пост бросаем
SOURCE_DEAD_RUNS = 12         # столько запусков подряд источник пуст -> алерт (12 = ~3 часа)
IDLE_ALERT_HOURS = 6          # днём нет публикаций столько часов -> алерт
# Что считать интересным. Это текст для ИИ, правьте его под свой канал:
INTEREST_CRITERIA = (
    "Аудитория канала читает российские новости. Выше оценивай: громкие и важные "
    "события в стране, скандалы, заявления и конфликты известных людей, новости про "
    "блогеров, стримеров и шоу-бизнес, деньги, цены и законы, которые касаются многих. "
    "Ниже оценивай: мелкие местные происшествия, сухие сводки, посты без конкретных "
    "фактов, шутки, мемы и пересказы без новой информации."
)

GEMINI_MODEL = "gemini-2.5-flash"
GROQ_MODEL = "llama-3.3-70b-versatile"
OPENROUTER_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-3-27b-it:free",
]
# =====================================================

STATE_FILE = "state.json"
MSK = ZoneInfo("Europe/Moscow")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

AD_MARKERS = ["erid", "#реклама", "на правах рекламы", "промокод", "розыгрыш"]
PROMO_LINE = re.compile(
    r"подписывайтесь|подписаться|подпишись|подпишитесь|наш канал|наш чат|"
    r"прислать новост|предложить новост|прислать нам",
    re.I,
)
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50\u2B06\uFE0F\u200d]"
)

PROMPT = """Ты редактор новостного канала. Перепиши новость своими словами, \
сохранив все факты, имена, цифры и даты. Ничего не выдумывай и не добавляй от себя.
Требования:
- длина примерно {n} символов (допустимо плюс-минус 20 процентов)
- без эмодзи, без хэштегов, без ссылок и упоминаний через @
- без обращений к читателям и без призывов подписаться
- нейтральный информационный стиль, русский язык
- верни только текст поста, без пояснений и вступлений
Если текст является рекламой, розыгрышем, приглашением подписаться или не \
является новостью, ответь одним словом: SKIP

Последние новости, уже опубликованные в канале:
{recent}

Если новость в тексте ниже описывает то же событие, что одна из опубликованных \
выше (пусть другими словами или с другими подробностями), ответь одним словом: DUPLICATE

Текст:
{text}
"""


# ---------------------- состояние ----------------------
def load_state():
    """Нет файла -> пустое состояние. Файл есть, но битый -> остановка:
    пустое состояние обнулило бы seen и бот опубликовал бы старое повторно."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state.json не объект")
        return data
    except Exception as e:
        alert(f"state.json повреждён ({e}), бот остановлен, чтобы не дублировать посты")
        sys.exit(1)


def save_state(state):
    state["seen"] = state.get("seen", [])[-4000:]
    state["recent"] = state.get("recent", [])[-60:]
    state["posts"] = state.get("posts", [])[-40:]
    scores = state.get("scores", {})
    if len(scores) > 400:
        state["scores"] = dict(list(scores.items())[-400:])
    state["media_hist"] = state.get("media_hist", [])[-50:]
    fails = state.get("fails", {})
    if len(fails) > 200:
        state["fails"] = dict(list(fails.items())[-200:])
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)  # запись целиком или никак


# ---------------------- Telegram ----------------------
def tg(method, files=None, **data):
    r = None
    for _ in range(3):
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            data=data,
            files=files,
            timeout=180,
        )
        if r.status_code == 429:  # слишком часто: ждём, сколько просит Telegram
            try:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
            except ValueError:
                wait = 5
            time.sleep(min(int(wait) + 1, 30))
            continue
        break
    if not r.ok:
        raise RuntimeError(f"Telegram {method}: {r.status_code} {r.text[:200]}")
    try:
        return r.json().get("result")
    except ValueError:
        return None


def alert(message):
    """Сообщение администратору (если задан секрет ADMIN_CHAT_ID)."""
    print("ALERT:", message)
    if ADMIN_CHAT_ID and BOT_TOKEN:
        try:
            tg("sendMessage", chat_id=ADMIN_CHAT_ID, text=f"Бот канала: {message}")
        except Exception as e:
            print("Не удалось отправить алерт:", e)


def download(url):
    r = requests.get(url, headers=HEADERS, timeout=120)
    r.raise_for_status()
    if len(r.content) > 49 * 1024 * 1024:
        raise RuntimeError("файл больше 49 МБ")
    return r.content


def send_media(media, caption, upload):
    """Одно фото/видео или альбом. upload=False: Telegram сам скачивает по ссылке,
    upload=True: скрипт скачивает файлы и загружает их сам (надёжнее для больших)."""
    files = {}
    if len(media) == 1:
        item = media[0]
        is_photo = item["type"] == "photo"
        field = "photo" if is_photo else "video"
        data = {"chat_id": CHANNEL}
        if caption:
            data["caption"] = caption
        if not is_photo:
            data["supports_streaming"] = "true"
        if upload:
            files[field] = (f"file.{'jpg' if is_photo else 'mp4'}", download(item["url"]))
        else:
            data[field] = item["url"]
        tg("sendPhoto" if is_photo else "sendVideo", files=files or None, **data)
        return

    group = []
    for i, item in enumerate(media):
        is_photo = item["type"] == "photo"
        entry = {"type": item["type"]}
        if upload:
            files[f"f{i}"] = (f"f{i}.{'jpg' if is_photo else 'mp4'}", download(item["url"]))
            entry["media"] = f"attach://f{i}"
        else:
            entry["media"] = item["url"]
        if i == 0 and caption:
            entry["caption"] = caption
        group.append(entry)
    tg("sendMediaGroup", files=files or None, chat_id=CHANNEL, media=json.dumps(group))


def publish(text, media):
    head = f"{HEADER_EMOJI} " if HEADER_EMOJI else ""
    final = head + text + (f"\n\n{SIGNATURE}" if SIGNATURE else "")
    media = media[:10] if USE_MEDIA else []
    if media:
        caption = final if len(final) <= 1024 else None  # лимит подписи в Telegram
        sent = False
        for upload in (False, True):
            try:
                send_media(media, caption, upload)
                sent = True
                break
            except Exception as e:
                how = "с загрузкой" if upload else "по ссылке"
                print(f"Медиа не отправилось ({how}): {e}")
        if sent:
            if caption is None:  # текст длиннее 1024 знаков идёт следом отдельным сообщением
                tg("sendMessage", chat_id=CHANNEL, text=final, disable_web_page_preview="true")
            return
        # пост с медиа не превращаем в текстовый: повторим при следующем запуске
        raise RuntimeError("медиа не отправилось ни по ссылке, ни с загрузкой")
    tg("sendMessage", chat_id=CHANNEL, text=final, disable_web_page_preview="true")


# ---------------------- ручная модерация ----------------------
# Как это работает. Бот живёт в GitHub Actions и не умеет ждать нажатий, поэтому:
#  1) готовый пост уходит тебе в личку с кнопками и запоминается в state["pending"];
#  2) при каждом следующем запуске бот забирает твои нажатия и ответы (getUpdates)
#     и сразу выполняет их: публикует, отклоняет, переключает медиа.
# Задержка между нажатием и публикацией: до одного интервала запуска (до 15 минут).
def mod_media_summary(media):
    photos = sum(1 for m in media if m["type"] == "photo")
    videos = sum(1 for m in media if m["type"] == "video")
    parts = []
    if photos:
        parts.append(f"{photos} фото")
    if videos:
        parts.append(f"{videos} видео")
    return ", ".join(parts) or "нет"


def mod_buttons(pid, item):
    rows = [[
        {"text": "✅ Опубликовать", "callback_data": f"ok:{pid}"},
        {"text": "❌ Отклонить", "callback_data": f"no:{pid}"},
    ]]
    if item.get("media"):
        on = "вкл" if item.get("media_on", True) else "выкл"
        rows.append([{"text": f"🖼 Медиа: {on} (нажми, чтобы переключить)", "callback_data": f"md:{pid}"}])
    return json.dumps({"inline_keyboard": rows})


def mod_preview(item):
    body = item["text"]
    if len(body) > 3200:
        body = body[:3200] + "…"
    head = f"{HEADER_EMOJI} " if HEADER_EMOJI else ""
    score = item.get("ai")
    return (
        f"На модерацию · {item['source']} · оценка {score if score is not None else '?'}\n"
        f"Медиа: {mod_media_summary(item['media'])}\n"
        f"Оригинал: https://t.me/{item['src_id']}\n\n"
        f"{head}{body}\n\n"
        "Ответом на это сообщение можно заменить текст или фото. "
        "Пост с твоей правкой выйдет сразу."
    )


def mod_close(item, label):
    """Убирает кнопки и подписывает итог под превью."""
    try:
        tg(
            "editMessageText",
            chat_id=ADMIN_CHAT_ID,
            message_id=item["msg_id"],
            text=f"{label}\n\n{item['text'][:300]}",
            disable_web_page_preview="true",
        )
    except Exception as e:
        print(f"Не удалось обновить сообщение модерации: {e}")


def send_for_moderation(state, now, p, text):
    state["pend_seq"] = state.get("pend_seq", 0) + 1
    pid = f"p{state['pend_seq']}"
    item = {
        "text": text,
        "media": (p["media"][:10] if USE_MEDIA else []),
        "media_on": True,
        "source": p["id"].split("/")[0],
        "src_id": p["id"],
        "ai": p.get("ai"),
        "created": now.isoformat(),
        "msg_id": None,
    }
    res = tg(
        "sendMessage",
        chat_id=ADMIN_CHAT_ID,
        text=mod_preview(item),
        reply_markup=mod_buttons(pid, item),
        disable_web_page_preview="true",
    )
    if not res or "message_id" not in res:
        raise RuntimeError("Telegram не вернул номер сообщения")
    item["msg_id"] = res["message_id"]
    state["pending"][pid] = item
    return pid


def effective_moderate(state, now, is_day):
    """Нужно ли сейчас присылать посты на одобрение. Возвращает (да/нет, вид, пояснение).
    Вид: manual | auto | night | idle | noadmin."""
    mode = state.get("mode") or ("manual" if MODERATION else "auto")
    if not ADMIN_CHAT_ID:
        return False, "noadmin", "не задан ADMIN_CHAT_ID, модерация невозможна"
    if mode != "manual":
        return False, "auto", "включена автопубликация"
    if not (is_day or MODERATE_AT_NIGHT):
        return False, "night", "ночью без модерации"
    try:
        idle = now - datetime.fromisoformat(state["last_admin"])
    except (KeyError, ValueError):
        idle = timedelta(0)
    if idle > timedelta(hours=MANUAL_IDLE_HOURS):
        return False, "idle", f"нет активности {int(idle.total_seconds() // 3600)} ч"
    return True, "manual", ""


def handle_command(state, text, now, is_day):
    """Команды админа: /manual, /auto, /status."""
    cmd = text.split()[0].split("@")[0].lower()
    if cmd == "/auto":
        state["mode"] = "auto"
        reply = "Режим: автопубликация. Бот сам выкладывает лучшие посты. Вернуть ручную модерацию: /manual"
    elif cmd == "/manual":
        state["mode"] = "manual"
        reply = (
            "Режим: ручная модерация. Посты приходят сюда с кнопками. "
            f"Если ты ничего не нажмёшь и не напишешь {MANUAL_IDLE_HOURS} ч, бот временно перейдёт на автопубликацию. "
            "Любое твоё действие вернёт ручной режим. Автопубликация насовсем: /auto"
        )
    elif cmd in ("/status", "/start", "/help"):
        mode = state.get("mode") or ("manual" if MODERATION else "auto")
        moderate, _, why = effective_moderate(state, now, is_day)
        waiting = sum(1 for v in state["pending"].values() if not v.get("decision"))
        reply = (
            f"Режим: {'ручная модерация' if mode == 'manual' else 'автопубликация'}\n"
            f"Сейчас работает: {'ручная модерация' if moderate else 'автопубликация (' + why + ')'}\n"
            f"Ждут твоего решения: {waiting}\n"
            f"Опубликовано сегодня: {state.get('count', 0)}/{DAILY_LIMIT}\n\n"
            "Команды: /manual (ручная модерация), /auto (автопубликация), /status. "
            "Команды выполняются при ближайшем запуске бота, это до 15 минут."
        )
    else:
        return
    try:
        tg("sendMessage", chat_id=ADMIN_CHAT_ID, text=reply)
    except Exception as e:
        print(f"Не удалось ответить на команду: {e}")


def handle_update(state, u, now, is_day):
    """Одно обновление от Telegram: нажатие кнопки, команда или ответ на превью. Чужих игнорируем."""
    pend = state["pending"]
    cq = u.get("callback_query")
    if cq:
        # свой: личка админа (id человека == id чата) или общая группа модерации (id чата == ADMIN_CHAT_ID)
        cq_chat = (cq.get("message") or {}).get("chat", {}).get("id")
        if str(cq.get("from", {}).get("id")) != str(ADMIN_CHAT_ID) and str(cq_chat) != str(ADMIN_CHAT_ID):
            return
        state["last_admin"] = now.isoformat()
        action, _, pid = (cq.get("data") or "").partition(":")
        item = pend.get(pid)
        note = ""
        if not item:
            note = "Пост уже обработан или устарел"
        elif action == "ok":
            item["decision"] = "ok"
            note = "Принято, публикую"
        elif action == "no":
            item["decision"] = "no"
            note = "Отклонено"
        elif action == "md":
            item["media_on"] = not item.get("media_on", True)
            note = "Медиа включены" if item["media_on"] else "Медиа выключены"
            try:
                tg(
                    "editMessageReplyMarkup",
                    chat_id=ADMIN_CHAT_ID,
                    message_id=item["msg_id"],
                    reply_markup=mod_buttons(pid, item),
                )
            except Exception as e:
                print(f"Не удалось обновить кнопки: {e}")
        try:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text=note)
        except Exception:
            pass  # старые нажатия Telegram уже не даёт подтвердить, это не страшно
        return

    msg = u.get("message")
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    chat_id = msg.get("chat", {}).get("id")
    if text.startswith("/") and text.split()[0].split("@")[0].lower() == "/id":
        # подсказка для настройки: любой чат, где написали /id, узнаёт свой номер
        try:
            tg("sendMessage", chat_id=chat_id, text=f"ID этого чата: {chat_id}")
        except Exception as e:
            print(f"Не удалось ответить на /id: {e}")
        return
    if str(chat_id) != str(ADMIN_CHAT_ID):
        return
    state["last_admin"] = now.isoformat()  # любое сообщение модератора считается активностью
    if text.startswith("/"):
        handle_command(state, text, now, is_day)
        return
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    if not reply_to:
        return
    pid = next((k for k, v in pend.items() if v.get("msg_id") == reply_to), None)
    if not pid:
        return
    item = pend[pid]
    new_text = (msg.get("text") or msg.get("caption") or "").strip()
    if HEADER_EMOJI and new_text.startswith(HEADER_EMOJI):
        new_text = new_text[len(HEADER_EMOJI):].strip()  # молнию бот добавит сам
    changed = False
    if msg.get("photo"):
        item["media"] = [{"type": "photo", "url": msg["photo"][-1]["file_id"]}]
        item["media_on"] = True
        changed = True
    elif msg.get("video"):
        item["media"] = [{"type": "video", "url": msg["video"]["file_id"]}]
        item["media_on"] = True
        changed = True
    if new_text:
        item["text"] = new_text[:3500]
        changed = True
    if changed:
        item["decision"] = "ok"
        print(f"{pid}: админ заменил текст/медиа, публикую")


def moderation_step(state, now, is_day):
    """Забирает нажатия, команды и ответы, публикует одобренное, снимает отклонённое и устаревшее.
    Возвращает (были ли ошибки публикации, нужна ли сейчас модерация, вид режима, пояснение)."""
    had_error = False
    updates = []
    if ADMIN_CHAT_ID:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                data={
                    "offset": state.get("tg_offset", 0),
                    "timeout": 0,
                    "allowed_updates": json.dumps(["callback_query", "message"]),
                },
                timeout=30,
            )
            r.raise_for_status()
            updates = r.json().get("result", [])
        except Exception as e:  # текст ошибки requests содержит адрес с токеном, его не печатаем
            code = getattr(getattr(e, "response", None), "status_code", "")
            print(f"Нажатия не получены ({type(e).__name__} {code}). Если 409: у бота включён webhook, его надо снять")
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        try:
            handle_update(state, u, now, is_day)
        except Exception as e:
            print(f"Обновление {u.get('update_id')} не разобрано: {type(e).__name__}")
    if updates:
        print(f"Модерация: получено обновлений {len(updates)}")

    moderate, kind, why = effective_moderate(state, now, is_day)  # после обработки команд

    # 1. устаревшие без решения снимаем
    for pid, item in list(state["pending"].items()):
        if item.get("decision"):
            continue
        try:
            age = now - datetime.fromisoformat(item["created"])
        except (KeyError, ValueError):
            age = timedelta(0)
        if age > timedelta(hours=PENDING_TTL_HOURS):
            mod_close(item, "⌛ Устарело, снято без публикации")
            del state["pending"][pid]
            print(f"{pid}: устарел ({int(age.total_seconds() // 3600)} ч), снят")

    # 2. без модерации (авто, ночь, молчание админа) самые старые неодобренные посты выпускаем сами
    if not moderate:
        waiting = sorted(
            (kv for kv in state["pending"].items() if not kv[1].get("decision")),
            key=lambda kv: kv[1].get("created", ""),
        )
        for pid, item in waiting[:AUTO_RELEASE_PER_RUN]:
            item["decision"] = "ok"
            print(f"{pid}: публикую без одобрения ({why})")

    # 3. исполняем решения
    done = 0
    for pid, item in list(state["pending"].items()):
        decision = item.get("decision")
        if decision == "no":
            mod_close(item, "❌ Отклонено")
            del state["pending"][pid]
        elif decision == "ok":
            if done > 0:
                time.sleep(random.uniform(POST_PAUSE_MIN_SEC, POST_PAUSE_MAX_SEC))
            media = item["media"] if item.get("media_on", True) else []
            try:
                publish(item["text"], media)
            except Exception as e:
                n = item.get("fails", 0) + 1
                item["fails"] = n
                alert(f"не удалось опубликовать пост {pid} (попытка {n}/{MAX_PUBLISH_FAILS}): {e}")
                if n >= MAX_PUBLISH_FAILS:
                    mod_close(item, "⚠️ Не удалось опубликовать")
                    del state["pending"][pid]
                had_error = True
                continue  # при n < MAX остаётся решение "ok": повторим при следующем запуске
            done += 1
            state["count"] = state.get("count", 0) + 1
            state["last_pub"] = now.isoformat()
            state["media_hist"].append(1 if media else 0)
            state["posts"].append(item["text"][:250].replace("\n", " "))
            mod_close(item, "✅ Опубликовано" if moderate else "✅ Опубликовано (без одобрения)")
            del state["pending"][pid]
            print(f"Опубликован пост {pid} из {item['src_id']}")
            save_state(state)  # сразу, чтобы сбой дальше не привёл к повторной публикации
    return had_error, moderate, kind, why


# ---------------------- сбор постов ----------------------
def get_page(name):
    """Страница канала с тремя попытками: t.me иногда отвечает ошибкой или тормозит."""
    last = None
    for attempt in range(3):
        try:
            r = requests.get(f"https://t.me/s/{name}", headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def fetch_source(name):
    soup = BeautifulSoup(get_page(name), "html.parser")
    posts = []
    for w in soup.select("div.tgme_widget_message"):
        post_id = w.get("data-post")
        text_el = w.select_one("div.tgme_widget_message_text")
        time_el = w.select_one("a.tgme_widget_message_date time")
        if not post_id or not text_el or not time_el or not time_el.get("datetime"):
            continue
        for br in text_el.find_all("br"):
            br.replace_with("\n")
        media = []
        for ph in w.select("a.tgme_widget_message_photo_wrap"):
            m = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", ph.get("style", ""))
            if m and m.group(1) not in [x["url"] for x in media]:
                media.append({"type": "photo", "url": m.group(1)})
        for v in w.select("video"):
            src_url = v.get("src")
            if src_url and src_url not in [x["url"] for x in media]:
                media.append({"type": "video", "url": src_url})
        big_video = bool(
            w.select_one(
                ".tgme_widget_message_video_player, .tgme_widget_message_roundvideo_player, "
                "a.tgme_widget_message_video_wrap"
            )
        ) and not any(x["type"] == "video" for x in media)
        views = None
        v_el = w.select_one("span.tgme_widget_message_views")
        if v_el:
            views = parse_views(v_el.get_text())
        try:
            date = datetime.fromisoformat(time_el["datetime"])
        except ValueError:
            continue
        posts.append(
            {
                "id": post_id,
                "text": text_el.get_text().strip(),
                "date": date,
                "media": media[:10],
                "views": views,
                "big_video": big_video,
            }
        )

    # популярность поста относительно обычных просмотров этого канала
    now_utc = datetime.now(timezone.utc)
    old = [
        p["views"]
        for p in posts
        if p["views"] and now_utc - p["date"] > timedelta(hours=3)
    ]
    base = statistics.median(old) if len(old) >= 3 else None
    for p in posts:
        p["rel"] = (p["views"] / base) if (base and p["views"]) else None
    return posts


def parse_views(s):
    """'1.2K' -> 1200, '3.4M' -> 3400000, '845' -> 845."""
    m = re.match(r"([\d.]+)\s*([KkMm]?)", s.strip().replace(",", "."))
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    return int(num * {"k": 1e3, "m": 1e6}.get(m.group(2).lower(), 1))


def views_bonus(p):
    """Мягкая добавка к баллу за популярность: от 0 до +1."""
    rel = p.get("rel")
    return min(1.0, 2 * rel) if rel is not None else 0.0


def clean(text):
    text = re.sub(r"https?://\S+|t\.me/\S+", "", text)
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln and not PROMO_LINE.search(ln)]
    text = "\n".join(lines)
    text = re.sub(r"@\w+", "", text)
    text = EMOJI.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def text_only_allowed(state, now):
    """Можно ли сейчас выложить пост без фото/видео: их доля среди последних публикаций ограничена."""
    hist = state.get("media_hist", [])[-MEDIA_HISTORY:]
    if (hist.count(0) + 1) / (len(hist) + 1) <= TEXT_ONLY_SHARE:
        return True
    last = state.get("last_pub")
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) > timedelta(hours=TEXT_ONLY_IDLE_HOURS)
    except ValueError:
        return True


def is_ad(text):
    low = text.lower()
    return any(m in low for m in AD_MARKERS)


def words(text):
    """Значимые слова, обрезанные до 6 букв (чтобы 'убили' и 'убит' считались одним словом)."""
    return {w[:6] for w in re.findall(r"\w+", text.lower()) if len(w) >= 5}


def is_duplicate(text, state):
    a = words(text)
    if len(a) < 6:
        return False
    for prev in state.get("recent", []):
        b = set(prev)
        if len(b) >= 6 and len(a & b) / min(len(a), len(b)) > DUP_THRESHOLD:
            return True
    return False


# ---------------------- ИИ ----------------------
# Модели у провайдеров часто меняются, поэтому список доступных моделей
# запрашивается у самого сервиса, а названия из настроек нужны только как запасные.
GEMINI_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GROQ_LIST_URL = "https://api.groq.com/openai/v1/models"
OPENROUTER_LIST_URL = "https://openrouter.ai/api/v1/models"

GEMINI_BAD = ["image", "tts", "audio", "live", "embed", "imagen", "veo", "thinking",
              "learnlm", "gemma", "robotics", "computer", "native"]
GROQ_BAD = ["whisper", "guard", "tts", "orpheus", "playai", "safeguard"]
GROQ_PREF = ["gpt-oss-120b", "llama-3.3-70b", "llama-4-maverick", "llama-4-scout",
             "qwen", "gpt-oss-20b", "llama-3.1-8b"]
OPENROUTER_BAD = ["image", "embed", "audio", "guard", "moderation", "lyria", "tts"]
OPENROUTER_PREF = ["gpt-oss-120b", "llama-3.3-70b", "qwen3", "deepseek", "gemma-3-27b",
                   "mistral", "llama", "gemma"]

_MODELS = {}
_DEAD = set()  # провайдеры, у которых в этом запуске кончилась квота или не принят ключ


def http_error_text(e):
    resp = getattr(e, "response", None)
    if resp is None:
        return ""
    return f"{resp.status_code} {resp.text[:200]}".replace("\n", " ")


def pref_index(name, prefs):
    low = name.lower()
    for i, p in enumerate(prefs):
        if p in low:
            return i
    return len(prefs)


def list_gemini_models():
    r = requests.get(
        GEMINI_LIST_URL,
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        params={"pageSize": 200},
        timeout=30,
    )
    r.raise_for_status()
    names = []
    for m in r.json().get("models", []):
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        name = m["name"].split("/")[-1]
        low = name.lower()
        if "flash" not in low or any(b in low for b in GEMINI_BAD):
            continue
        names.append(name)

    def key(n):
        low = n.lower()
        nums = re.findall(r"\d+(?:\.\d+)?", n)
        version = float(nums[0]) if nums else 0.0
        unstable = "preview" in low or "exp" in low
        return (not unstable, "lite" not in low, version)

    names.sort(key=key, reverse=True)
    return names[:3]


def list_groq_models():
    r = requests.get(
        GROQ_LIST_URL,
        headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
        timeout=30,
    )
    r.raise_for_status()
    ids = [
        m["id"]
        for m in r.json().get("data", [])
        if not any(b in m["id"].lower() for b in GROQ_BAD)
    ]
    ids.sort(key=lambda n: pref_index(n, GROQ_PREF))
    return ids[:3]


def list_openrouter_models():
    r = requests.get(OPENROUTER_LIST_URL, timeout=30)
    r.raise_for_status()
    ids = []
    for m in r.json().get("data", []):
        mid = m.get("id", "")
        arch = m.get("architecture") or {}
        if not mid.endswith(":free"):
            continue
        if any(b in mid.lower() for b in OPENROUTER_BAD):
            continue
        if "text" not in (arch.get("output_modalities") or ["text"]):
            continue
        if "text" not in (arch.get("input_modalities") or ["text"]):
            continue
        ids.append(mid)
    ids.sort(key=lambda n: pref_index(n, OPENROUTER_PREF))
    return ids[:5]


LISTERS = {
    "gemini": (list_gemini_models, lambda: [GEMINI_MODEL]),
    "groq": (list_groq_models, lambda: [GROQ_MODEL]),
    "openrouter": (list_openrouter_models, lambda: list(OPENROUTER_MODELS)),
}


# Дополнительные провайдеры с OpenAI-совместимым API. Включаются сами, если задан секрет с ключом.
# Чтобы добавить ещё одного, достаточно дописать блок в этот словарь.
EXTRA_PROVIDERS = {
    "cerebras": {
        "env": "CEREBRAS_API_KEY",
        "base": "https://api.cerebras.ai/v1",
        "fallback": ["gpt-oss-120b", "llama-3.3-70b"],
        "pref": ["gpt-oss-120b", "llama-3.3-70b", "qwen", "llama"],
        "bad": ["embed", "whisper", "guard"],
    },
    "mistral": {
        "env": "MISTRAL_API_KEY",
        "base": "https://api.mistral.ai/v1",
        "fallback": ["mistral-small-latest"],
        "pref": ["mistral-small", "mistral-medium", "open-mistral", "mistral-large", "ministral"],
        "bad": ["embed", "moderation", "ocr", "codestral", "voxtral", "pixtral", "devstral", "magistral"],
    },
    "sambanova": {
        "env": "SAMBANOVA_API_KEY",
        "base": "https://api.sambanova.ai/v1",
        "fallback": ["Meta-Llama-3.3-70B-Instruct"],
        "pref": ["gpt-oss-120b", "llama-3.3-70b", "deepseek", "qwen", "llama"],
        "bad": ["embed", "whisper", "guard", "vision", "audio"],
    },
    "nvidia": {  # build.nvidia.com, бесплатный ключ nvapi-...
        "env": "NVIDIA_API_KEY",
        "base": "https://integrate.api.nvidia.com/v1",
        "fallback": ["meta/llama-3.3-70b-instruct", "openai/gpt-oss-120b"],
        "pref": ["llama-3.3-70b", "gpt-oss-120b", "llama-3.1-70b", "qwen", "mistral", "llama"],
        "bad": ["embed", "rerank", "vision", "vila", "neva", "fuyu", "paligemma", "kosmos",
                "clip", "guard", "safety", "reward", "retriev", "parakeet", "riva", "nemoguard",
                "ocr", "nv-", "-vl", "deplot", "bge", "arctic-embed"],
    },
    "github": {  # GitHub Models; токен выдаёт сам GitHub Actions, регистрироваться не нужно
        "env": "GITHUB_MODELS_TOKEN",
        "base": "https://models.github.ai/inference",
        "fallback": ["openai/gpt-4.1-mini", "openai/gpt-4o-mini"],
        "pref": ["gpt-4.1-mini", "gpt-4o-mini", "llama-3.3-70b", "mistral"],
        "bad": ["embed", "o1", "o3", "o4"],
    },
    "cohere": {  # пробный ключ, лимиты небольшие, поэтому в самом конце цепочки
        "env": "COHERE_API_KEY",
        "base": "https://api.cohere.ai/compatibility/v1",
        "fallback": ["command-a-03-2025", "command-r-plus-08-2024"],
        "pref": ["command-a", "command-r-plus", "command-r"],
        "bad": ["embed", "rerank", "vision", "reasoning", "translate", "arabic"],
    },
}


def make_extra_lister(name):
    cfg = EXTRA_PROVIDERS[name]

    def lister():
        r = requests.get(
            f"{cfg['base']}/models",
            headers={"Authorization": f"Bearer {os.environ[cfg['env']]}"},
            timeout=30,
        )
        r.raise_for_status()
        ids = [
            m["id"]
            for m in r.json().get("data", [])
            if m.get("id") and not any(b in m["id"].lower() for b in cfg["bad"])
        ]
        ids.sort(key=lambda n: pref_index(n, cfg["pref"]))
        return ids[:3]

    return lister


for _name, _cfg in EXTRA_PROVIDERS.items():
    LISTERS[_name] = (make_extra_lister(_name), lambda c=_cfg: list(c["fallback"]))


def get_models(provider):
    if provider not in _MODELS:
        lister, default = LISTERS[provider]
        try:
            models = lister()
            print(f"Модели {provider}: {', '.join(models) or 'пусто'}")
            _MODELS[provider] = models or default()
        except Exception as e:
            print(f"Список моделей {provider} не получен ({type(e).__name__} {http_error_text(e)}), беру запасные")
            _MODELS[provider] = default()
    return _MODELS[provider]


def call_gemini(prompt, model):
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def call_openai_compat(url, key, model, prompt):
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def rewrite(text, recent_posts):
    """Пробует ИИ по очереди. Возвращает текст, "SKIP"/"DUPLICATE" или None, если все упали."""
    recent = "\n".join(f"- {t}" for t in recent_posts) or "(пока нет)"
    prompt = PROMPT.format(n=len(text), text=text, recent=recent)
    return ask_ai(prompt)


SCORE_PROMPT = """Ты главный редактор новостного канала. {criteria}
Оцени каждую новость по шкале от 1 до 10: насколько она важна и интересна читателям \
канала (10 = обязательно публиковать, 1 = совсем не нужна).
Верни только JSON-объект: ключ это номер новости, значение это балл. \
Пример: {"1": 7, "2": 3}

Новости:
"""


def parse_scores(raw, n):
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    out = {}
    for k, v in data.items():
        try:
            i, s = int(k), float(v)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= n:
            out[i] = max(1.0, min(10.0, s))
    return out or None


def score_posts(items):
    """Один запрос к ИИ: оценки сразу для всех постов. Возвращает {номер: балл} или None."""
    lines = [
        f"{i}. " + p["clean"][:300].replace("\n", " ") for i, p in enumerate(items, 1)
    ]
    prompt = SCORE_PROMPT.replace("{criteria}", INTEREST_CRITERIA) + "\n".join(lines)
    raw = ask_ai(prompt)
    if raw is None:
        return None
    return parse_scores(raw, len(items))


def ask_ai(prompt):
    """Пробует ИИ по очереди (Gemini, Groq, OpenRouter). Текст ответа или None, если все упали."""
    attempts = []  # (подпись, провайдер, модель, вызов)
    if os.getenv("GEMINI_API_KEY"):
        for m in get_models("gemini"):
            attempts.append((f"Gemini {m}", "gemini", m, lambda m=m: call_gemini(prompt, m)))
    if os.getenv("GROQ_API_KEY"):
        for m in get_models("groq"):
            attempts.append(
                (
                    f"Groq {m}",
                    "groq",
                    m,
                    lambda m=m: call_openai_compat(
                        GROQ_URL, os.environ["GROQ_API_KEY"], m, prompt
                    ),
                )
            )
    for name, cfg in EXTRA_PROVIDERS.items():
        if not os.getenv(cfg["env"]):
            continue
        for m in get_models(name):
            attempts.append(
                (
                    f"{name} {m}",
                    name,
                    m,
                    lambda m=m, cfg=cfg: call_openai_compat(
                        f"{cfg['base']}/chat/completions", os.environ[cfg["env"]], m, prompt
                    ),
                )
            )
    if os.getenv("OPENROUTER_API_KEY"):
        for m in get_models("openrouter"):
            attempts.append(
                (
                    f"OpenRouter {m}",
                    "openrouter",
                    m,
                    lambda m=m: call_openai_compat(
                        OPENROUTER_URL, os.environ["OPENROUTER_API_KEY"], m, prompt
                    ),
                )
            )
    for label, provider, model, fn in attempts:
        if provider in _DEAD:
            continue
        try:
            out = (fn() or "").strip()
            if out:
                print(f"ИИ: {label} ответил")
                models = _MODELS[provider]  # рабочую модель ставим первой
                if model in models:
                    models.remove(model)
                    models.insert(0, model)
                return out
        except Exception as e:
            print(f"ИИ: {label} не сработал ({type(e).__name__} {http_error_text(e)})")
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (401, 402, 403, 429):  # ключ/квота: остальные модели этого провайдера пропускаем
                _DEAD.add(provider)
                print(f"ИИ: {provider} до конца запуска пропускаю")
    return None


def postprocess(text):
    text = text.replace("**", "").replace("`", "")
    text = EMOJI.sub("", text)
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:3500]


# ---------------------- основной цикл ----------------------
def main():
    if not BOT_TOKEN:
        sys.exit("Не задан BOT_TOKEN")

    now = datetime.now(timezone.utc)
    msk = now.astimezone(MSK)
    is_day = DAY_FROM_HOUR <= msk.hour < DAY_TO_HOUR

    state = load_state()
    today = msk.strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"] = today
        state["count"] = 0
    state.setdefault("count", 0)
    state.setdefault("pending", {})     # посты, ожидающие решения админа
    state.setdefault("media_hist", [])
    state.setdefault("posts", [])
    mode = state.get("mode") or ("manual" if MODERATION else "auto")
    if mode == "manual" and not ADMIN_CHAT_ID:
        sys.exit("Включена ручная модерация, но не задан секрет ADMIN_CHAT_ID: некому присылать посты")
    # нажатия, команды и ответы обрабатываем всегда (и ночью, и при достигнутом лимите),
    # moderate = присылать ли сейчас новые посты тебе на одобрение, а не публиковать самому
    mod_error, moderate, kind, why = moderation_step(state, now, is_day)
    print(f"Режим: {kind}{' (' + why + ')' if why else ''}")
    # при уходе в автопубликацию из-за молчания предупреждаем один раз, чтобы это не было сюрпризом
    if kind == "idle" and not state.get("idle_notified"):
        state["idle_notified"] = True
        alert(f"тебя не было на связи, перехожу на автопубликацию ({why}). "
              "Любое твоё сообщение или нажатие вернёт ручную модерацию.")
    elif kind != "idle":
        state["idle_notified"] = False
    if state["count"] >= DAILY_LIMIT and not moderate:  # одобрение вручную лимитом не блокируется
        print("Дневной лимит достигнут")
        save_state(state)
        return

    state.setdefault("seen", [])
    state.setdefault("recent", [])
    state.setdefault("posts", [])
    state.setdefault("scores", {})
    state.setdefault("media_hist", [])  # 1 = опубликован пост с медиа, 0 = без медиа
    state.setdefault("fails", {})       # id -> число неудачных публикаций
    state.setdefault("src_dead", {})    # источник -> запусков подряд без постов
    seen = set(state["seen"])
    stats_reason = {}                   # причина отсева -> сколько постов

    def mark(post_id, reason=None):
        if post_id not in seen:
            seen.add(post_id)
            state["seen"].append(post_id)
        if reason:
            stats_reason[reason] = stats_reason.get(reason, 0) + 1

    # тишина: днём давно ничего не публиковалось
    last_pub = state.get("last_pub")
    if is_day and last_pub:
        try:
            idle = now - datetime.fromisoformat(last_pub)
            last_idle_alert = state.get("last_idle_alert")
            alert_gap_ok = (
                not last_idle_alert
                or now - datetime.fromisoformat(last_idle_alert) > timedelta(hours=IDLE_ALERT_HOURS)
            )
            if idle > timedelta(hours=IDLE_ALERT_HOURS) and alert_gap_ok:
                alert(f"уже {int(idle.total_seconds() // 3600)} ч нет публикаций")
                state["last_idle_alert"] = now.isoformat()
        except ValueError:
            pass

    max_age = MAX_AGE_DAY_HOURS if is_day else MAX_AGE_NIGHT_HOURS
    override = os.getenv("MAX_AGE_OVERRIDE", "").strip()
    if override:
        try:
            max_age = float(override)
            print(f"ТЕСТ: окно свежести {max_age} ч")
        except ValueError:
            pass
    print(f"Сейчас {msk:%H:%M} по Москве ({'день' if is_day else 'ночь'}), окно свежести {max_age} ч")

    candidates = []
    failed_sources = 0
    for source in dict.fromkeys(SOURCES):  # без дублей, порядок сохраняется
        try:
            posts = fetch_source(source)
        except Exception as e:
            print(f"Источник {source} не открылся: {type(e).__name__}")
            failed_sources += 1
            posts = []
        # источник пуст или недоступен много запусков подряд -> один алерт на каждые N запусков
        dead = state["src_dead"]
        if posts:
            dead[source] = 0
        else:
            dead[source] = dead.get(source, 0) + 1
            if dead[source] % SOURCE_DEAD_RUNS == 0:
                alert(f"источник {source} не отдаёт посты уже {dead[source]} запусков подряд "
                      f"(проверьте имя канала и что у него открыт веб-просмотр t.me/s/{source})")
        if not posts:
            continue
        stats = {"new": 0, "seen": 0, "old": 0}
        for p in posts:
            p["source"] = source
            if p["id"] in seen:
                stats["seen"] += 1
                continue
            if now - p["date"] > timedelta(hours=max_age):
                stats["old"] += 1
                continue
            stats["new"] += 1
            candidates.append(p)
        newest = min((now - p["date"] for p in posts), default=None)
        newest_txt = f"{int(newest.total_seconds() // 60)} мин назад" if newest else "нет постов"
        print(
            f"{source}: постов с текстом на странице {len(posts)}, самый свежий {newest_txt}; "
            f"новых в окне {stats['new']}, уже обработано {stats['seen']}, старше окна {stats['old']}"
        )

    if failed_sources == len(set(SOURCES)):
        alert("ни один источник не открылся")
    print(f"Кандидатов: {len(candidates)}")

    # 1. быстрые фильтры (длина, реклама, повторы по словам)
    candidates.sort(key=lambda p: p["date"], reverse=True)  # свежие первыми
    pool = []
    for p in candidates:
        text = clean(p["text"])
        reason = None
        if len(text) < MIN_LENGTH:
            reason = "слишком короткий"
        elif is_ad(text):
            reason = "реклама"
        elif is_duplicate(text, state):
            reason = "повтор уже опубликованного"
        if reason:
            print(f"{p['id']}: отсеян ({reason})")
            mark(p["id"], reason)
            continue
        p["clean"] = text
        pool.append(p)
        if p["big_video"]:
            print(f"{p['id']}: видео слишком большое для веб-превью, публикую без него")
    pool = pool[:POOL_SIZE]

    # 2. оценка важности ИИ (один запрос на все новые посты, оценки запоминаются)
    cached = state["scores"]
    to_score = [p for p in pool if p["id"] not in cached]
    if to_score:
        scores = score_posts(to_score)
        if scores is None:
            print("Оценка ИИ недоступна, выбираю по свежести")
        else:
            for i, p in enumerate(to_score, 1):
                if i in scores:
                    cached[p["id"]] = scores[i]
            print("Оценки: " + ", ".join(f"{p['id']}={cached.get(p['id'], '?')}" for p in to_score))

    ranked = []
    for p in pool:
        p["ai"] = cached.get(p["id"])
        # в ручном режиме решаешь ты: оценка ИИ нужна только для порядка и показывается в превью
        if not moderate and p["ai"] is not None and p["ai"] < MIN_SCORE:
            print(f"{p['id']}: оценка {p['ai']:g} ниже порога {MIN_SCORE}, пропускаю")
            mark(p["id"], "низкая оценка")  # неинтересное больше не рассматриваем
            continue
        base = p["ai"] if p["ai"] is not None else MIN_SCORE
        p["rank"] = base + views_bonus(p) - (0 if (p["media"] or moderate) else TEXT_ONLY_PENALTY)
        ranked.append(p)
    ranked.sort(key=lambda p: p["rank"], reverse=True)  # при равенстве остаётся порядок "свежие первыми"

    # 3. рерайт и публикация лучших
    # сколько публиковать за запуск: доля очереди, но в пределах [MIN, MAX]
    per_run = 0
    if moderate:
        # ручной режим: больше постов (MANUAL_QUEUE_SHARE), но не больше свободных мест в очереди модерации
        if ranked:
            per_run = max(MIN_POSTS_PER_RUN, math.ceil(len(ranked) * MANUAL_QUEUE_SHARE))
        per_run = min(per_run, max(0, MAX_PENDING - len(state["pending"])))
    elif ranked:
        per_run = max(MIN_POSTS_PER_RUN, min(MAX_POSTS_PER_RUN, math.ceil(len(ranked) * QUEUE_SHARE)))
    action = "отправляю на модерацию" if moderate else "публикую"
    print(f"В очереди {len(ranked)}, {action} за запуск до {per_run} (ждут решения: {len(state['pending'])})")
    published = 0
    had_error = False

    for p in ranked:
        if published >= per_run or (not moderate and state["count"] >= DAILY_LIMIT):
            break
        # пост без фото/видео выходит, только пока их доля в канале не превышена (в ручном режиме решаешь ты)
        if not moderate and not p["media"] and not text_only_allowed(state, now):
            print(f"{p['id']}: без медиа, лимит таких постов исчерпан, оставляю в очереди")
            continue
        if published > 0 and not moderate:
            pause = random.uniform(POST_PAUSE_MIN_SEC, POST_PAUSE_MAX_SEC)
            print(f"Пауза {pause:.0f} с до следующего поста")
            time.sleep(pause)

        # за этот же запуск мог выйти пост про то же событие из другого источника
        if is_duplicate(p["clean"], state):
            print(f"{p['id']}: повтор только что опубликованного")
            mark(p["id"], "повтор в этом запуске")
            continue

        new = rewrite(p["clean"], state["posts"][-RECENT_FOR_AI:])
        if new is None:
            alert("все ИИ не ответили, повторю при следующем запуске")
            had_error = True
            break
        if new.upper().startswith(("SKIP", "DUPLICATE")):
            print(f"{p['id']}: пропущен ИИ ({new[:9]})")
            mark(p["id"], f"ИИ: {new[:9].upper().strip('. ')}")
            continue
        new = postprocess(new)
        if len(new) < 15 or len(new) > 2.5 * len(p["clean"]) + 200:
            print(f"{p['id']}: ответ ИИ выглядит странно (длина {len(new)}), пропускаю")
            mark(p["id"], "странный ответ ИИ")
            continue

        if moderate:
            try:
                pid = send_for_moderation(state, now, p, new)
            except Exception as e:
                alert(f"не удалось отправить пост {p['id']} на модерацию: {e}")
                had_error = True
                break
            mark(p["id"])
            # похожие новости из других источников больше не предлагаем
            state["recent"].append(sorted(words(new))[:200])
            published += 1
            print(f"На модерацию: {pid} из {p['id']} (оценка {p['ai']})")
            save_state(state)
            continue

        try:
            publish(new, p["media"])
        except Exception as e:
            n = state["fails"].get(p["id"], 0) + 1
            state["fails"][p["id"]] = n
            alert(f"не удалось опубликовать {p['id']} (попытка {n}/{MAX_PUBLISH_FAILS}): {e}")
            if n >= MAX_PUBLISH_FAILS:
                mark(p["id"], "не публикуется")
            had_error = True
            break

        mark(p["id"])
        state["count"] += 1
        state["last_pub"] = now.isoformat()
        state["media_hist"].append(1 if p["media"] else 0)
        state["recent"].append(sorted(words(new))[:200])
        state["posts"].append(new[:250].replace("\n", " "))
        published += 1
        print(f"Опубликован пост из {p['id']} (оценка {p['ai']}, просмотры {p['views']})")
        save_state(state)  # сразу, чтобы сбой дальше не привёл к повторной публикации

    reasons = ", ".join(f"{k}: {v}" for k, v in stats_reason.items()) or "нет"
    what = "отправлено на модерацию" if moderate else "опубликовано"
    print(f"ИТОГ: {what} {published}, в очереди осталось {max(0, len(ranked) - published)}, "
          f"отсеяно ({reasons}), за сутки опубликовано {state['count']}/{DAILY_LIMIT}")
    save_state(state)
    if had_error or mod_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
