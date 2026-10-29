"""Бот для канала @AlMaNewsToday.

Собирает свежие посты из публичных телеграм-каналов, переписывает их через ИИ
(Gemini -> Groq -> OpenRouter, если один не отвечает, берётся следующий)
и публикует в канал. Запускается по расписанию из GitHub Actions.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ===================== НАСТРОЙКИ =====================
CHANNEL = "@AlMaNewsToday"
SOURCES = ["topor", "uranews", "theohdaily", "Dyadyabatya0"]  # username без @
SIGNATURE = "@AlMaNewsToday"  # подпись в конце поста; "" = без подписи
USE_MEDIA = True              # брать все фото и видео из исходного поста
MAX_AGE_DAY_HOURS = 1         # днём берём только посты не старше часа
MAX_AGE_NIGHT_HOURS = 2       # ночью запуски реже, поэтому окно шире
MIN_LENGTH = 80               # слишком короткие посты пропускаем
DAY_FROM_HOUR = 8             # "день" по Москве: с 08:00 до 23:00
DAY_TO_HOUR = 23              # (частоту запусков задаёт cron в news.yml)
NIGHT_POSTS_PER_RUN = 2       # ночью запуск раз в час, поэтому можно 2 поста за раз
DAILY_LIMIT = 60              # максимум постов в сутки
RECENT_FOR_AI = 15            # сколько последних постов показывать ИИ для проверки повторов

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
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    state["seen"] = state.get("seen", [])[-3000:]
    state["recent"] = state.get("recent", [])[-40:]
    state["posts"] = state.get("posts", [])[-30:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ---------------------- Telegram ----------------------
def tg(method, files=None, **data):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=data,
        files=files,
        timeout=180,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram {method}: {r.status_code} {r.text[:200]}")


def alert(message):
    """Сообщение администратору (если задан секрет ADMIN_CHAT_ID)."""
    print("ALERT:", message)
    if ADMIN_CHAT_ID:
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
    final = text + (f"\n\n{SIGNATURE}" if SIGNATURE else "")
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
        print("Публикую только текстом")
    tg("sendMessage", chat_id=CHANNEL, text=final, disable_web_page_preview="true")


# ---------------------- сбор постов ----------------------
def fetch_source(name):
    r = requests.get(f"https://t.me/s/{name}", headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
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
        if w.select_one(
            ".tgme_widget_message_video_player, .tgme_widget_message_roundvideo_player, "
            "a.tgme_widget_message_video_wrap"
        ) and not any(x["type"] == "video" for x in media):
            print(f"{post_id}: видео слишком большое для веб-превью, пропускаю его")
        posts.append(
            {
                "id": post_id,
                "text": text_el.get_text().strip(),
                "date": datetime.fromisoformat(time_el["datetime"]),
                "media": media[:10],
            }
        )
    return posts


def clean(text):
    text = re.sub(r"https?://\S+|t\.me/\S+", "", text)
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln and not PROMO_LINE.search(ln)]
    text = "\n".join(lines)
    text = re.sub(r"@\w+", "", text)
    text = EMOJI.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def is_ad(text):
    low = text.lower()
    return any(m in low for m in AD_MARKERS)


def words(text):
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) >= 5}


def is_duplicate(text, state):
    a = words(text)
    if not a:
        return False
    for prev in state.get("recent", []):
        b = set(prev)
        if b and len(a & b) / min(len(a), len(b)) > 0.5:
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
    if state["count"] >= DAILY_LIMIT:
        print("Дневной лимит достигнут")
        save_state(state)
        return

    state.setdefault("seen", [])
    state.setdefault("recent", [])
    state.setdefault("posts", [])
    seen = set(state["seen"])

    def mark(post_id):
        if post_id not in seen:
            seen.add(post_id)
            state["seen"].append(post_id)

    max_age = MAX_AGE_DAY_HOURS if is_day else MAX_AGE_NIGHT_HOURS
    candidates = []
    failed_sources = 0
    for source in SOURCES:
        try:
            posts = fetch_source(source)
        except Exception as e:
            print(f"Источник {source} не открылся: {type(e).__name__}")
            failed_sources += 1
            continue
        for p in posts:
            if p["id"] in seen:
                continue
            if now - p["date"] > timedelta(hours=max_age):
                continue
            candidates.append(p)

    if failed_sources == len(SOURCES):
        alert("ни один источник не открылся")
    print(f"Кандидатов: {len(candidates)}")

    candidates.sort(key=lambda p: p["date"], reverse=True)  # свежие первыми
    per_run = (2 if len(candidates) > 15 else 1) if is_day else NIGHT_POSTS_PER_RUN
    published = 0
    had_error = False

    for p in candidates:
        if published >= per_run or state["count"] >= DAILY_LIMIT:
            break
        text = clean(p["text"])
        if len(text) < MIN_LENGTH or is_ad(text) or is_duplicate(text, state):
            mark(p["id"])
            continue

        new = rewrite(text, state["posts"][-RECENT_FOR_AI:])
        if new is None:
            alert("все ИИ не ответили, повторю при следующем запуске")
            had_error = True
            break
        if new.upper().startswith(("SKIP", "DUPLICATE")):
            print(f"{p['id']}: пропущен ИИ ({new[:9]})")
            mark(p["id"])
            continue
        new = postprocess(new)
        if len(new) < 40:
            mark(p["id"])
            continue

        try:
            publish(new, p["media"])
        except Exception as e:
            alert(f"не удалось опубликовать: {e}")
            had_error = True
            break

        mark(p["id"])
        state["count"] += 1
        state["recent"].append(sorted(words(new))[:80])
        state["posts"].append(new[:250].replace("\n", " "))
        published += 1
        print(f"Опубликован пост из {p['id']}")
        save_state(state)

    save_state(state)
    if had_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
