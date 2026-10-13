#!/usr/bin/env python3
"""Бот записи для студии эпиляции «Фрида» (Ижевск). Только стандартная библиотека Python.
Запуск:  python3 bot.py     Остановка: Ctrl+C
Настройки лежат в файле .env рядом со скриптом (BOT_TOKEN=..., OWNER_ID=...)."""
import html
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
ENV = HERE / ".env"


def read_env():
    out = {}
    if ENV.exists():
        for line in ENV.read_text("utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


CFG = read_env()
TOKEN = os.environ.get("BOT_TOKEN") or CFG.get("BOT_TOKEN", "")
OWNER = int(os.environ.get("OWNER_ID") or CFG.get("OWNER_ID") or 0)  # кому приходят все записи (если пусто — берётся из хранилища)
DATABASE_URL = os.environ.get("DATABASE_URL") or CFG.get("DATABASE_URL", "")  # Neon Postgres; пусто = хранить в файле
OWNER_CODE = CFG.get("OWNER_CODE", "frida2026")  # владелец студии пишет боту: /owner frida2026
API = f"https://api.telegram.org/bot{TOKEN}/"
TZ = timezone(timedelta(hours=4))  # Ижевск, UTC+4

# ---- Студия: всё, что можно менять, лежит здесь ----
STUDIO = "Фрида"
ADDRESS = "Ижевск, проспект Конструктора М. Т. Калашникова, 25, 1 этаж (ЖК «Матрёшки»)"
PHONE = "+7 (912) 851-02-80"
CONTACT_TG = "@frida_estetica"
CHANNEL = "https://t.me/frida_clinic"
VK = "https://vk.com/frida_clinic"
MAPS = "https://yandex.ru/maps/org/frida/186207404484/"
OPEN_H, CLOSE_H = 9, 20          # студия открыта 8:00–20:30 (2ГИС); записываем с 9:00 до 20:00
STEP = 30                        # шаг записи, минут
DAYS_AHEAD = 14                  # на сколько дней вперёд можно записаться
WORK_DAYS = (0, 1, 2, 3, 4, 5, 6)  # 0 = понедельник; уберите 6, если в воскресенье закрыто
SERVICES = [  # (название, минут, цена ₽)
    ("Электроэпиляция · 30 мин", 30, 800),
    ("Электроэпиляция · 60 мин", 60, 1600),
    ("Электроэпиляция · 90 мин", 90, 2400),
]
REMIND = [(24 * 60, "завтра"), (120, "через 2 часа")]  # за сколько минут напоминать клиенту

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
SHORT_DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
SHORT_MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
STATUS = {"new": "🆕 Новая", "confirmed": "✅ Подтверждена", "moved": "🔁 Перенесена", "cancelled": "❌ Отменена"}
BUSY = ("new", "confirmed", "moved")

GREETING = (f"Здравствуйте! 🌸 Добро пожаловать в студию «{STUDIO}».\n\n"
            "Электроэпиляция — единственный способ избавиться от нежелательных волос навсегда.\n\n"
            "Выберите, что вас интересует 👇")
DESCRIPTION = (f"🌸 Студия электроэпиляции «{STUDIO}», Ижевск\n\n"
               "Навсегда избавим от нежелательных волос бережно и безопасно.\n\n"
               "⭐ 5.0 на Яндекс Картах · 136 оценок\n"
               "💰 От 800 ₽ за 30 минут\n"
               "🕗 Ежедневно, по записи\n"
               "📍 Проспект Калашникова, 25\n"
               "✅ Онлайн-запись за минуту и напоминание перед визитом\n\n"
               "Нажмите «Запустить» (Start), чтобы выбрать удобное время.")
SHORT_DESCRIPTION = "Студия электроэпиляции в Ижевске. Запись за минуту, напоминание о визите 🌸"

esc = lambda s: html.escape(str(s or ""), quote=False)
rub = lambda n: f"{n:,}".replace(",", " ") + " ₽"
from_rub = lambda n: "от " + rub(n)  # цена зависит от мастера
DB = HERE / "bookings.json"
LOCK = threading.RLock()


def tg(method, **params):
    req = urllib.request.Request(API + method, json.dumps(params).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False}
    except Exception as e:
        print("сеть:", e)
        return {"ok": False}


def tg_photo(method, field, path, **params):
    """Отправка файла (multipart), нужна для аватарки."""
    b = uuid.uuid4().hex
    body = b""
    for k, v in params.items():
        body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{Path(path).name}"\r\n'
             f'Content-Type: image/jpeg\r\n\r\n').encode() + Path(path).read_bytes() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(API + method, body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False}


def now():
    return datetime.now(TZ)


def kb(rows):
    return {"inline_keyboard": rows}


def btn(text, data):
    return {"text": text, "callback_data": data}


def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


BACK = [btn("‹ В меню", "menu")]
BOOK = [btn("🌸 Записаться", "b")]


def menu(chat):
    if OWNER and chat == OWNER:
        return kb([[btn("📋 Записи", "adm")], [btn("📆 Сегодня и завтра", "agenda")]])
    return kb([
        [btn("🌸 Записаться", "b")],
        [btn("💰 Услуги и цены", "price"), btn("ℹ️ О методе", "about")],
        [btn("📍 Как добраться", "place"), btn("☎️ Связаться", "contact")],
        [btn("📅 Моя запись", "my")],
    ])


def greeting(chat):
    return "Здравствуйте! Здесь собраны все записи клиентов студии." if OWNER and chat == OWNER else GREETING


# ---------- записи ----------
STATE = HERE / "state.json"


def neon(query, params=()):
    """SQL к Neon по HTTPS (без сторонних библиотек)."""
    host = DATABASE_URL.split("@", 1)[1].split("/", 1)[0].split(":")[0]
    req = urllib.request.Request(f"https://{host}/sql", json.dumps({"query": query, "params": list(params)}).encode(),
                                 {"Content-Type": "application/json", "Neon-Connection-String": DATABASE_URL})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def kv_get(key, default=None):
    """Хранилище «ключ → значение»: в Neon на хостинге, в файле локально."""
    if DATABASE_URL:
        rows = neon("SELECT value FROM kv WHERE key = $1", [key]).get("rows", [])
        return json.loads(rows[0]["value"]) if rows else default
    try:
        return json.loads(STATE.read_text("utf-8")).get(key, default)
    except Exception:
        return default


def kv_set(key, value):
    if DATABASE_URL:
        neon("INSERT INTO kv(key, value) VALUES($1, $2) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
             [key, json.dumps(value, ensure_ascii=False)])
        return
    try:
        data = json.loads(STATE.read_text("utf-8"))
    except Exception:
        data = {}
    data[key] = value
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


def load():
    return kv_get("bookings", [])


def save(items):
    kv_set("bookings", items)


def find(items, bid):
    return next((b for b in items if b["id"] == bid), None)


def start_dt(b):
    return datetime.fromisoformat(f"{b['date']}T{b['time']}").replace(tzinfo=TZ)


def when(b):
    d = date.fromisoformat(b["date"])
    return f"{DAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}, {b['time']}"


def day_label(iso):
    d = date.fromisoformat(iso)
    return f"{DAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def day_short(iso):
    d = date.fromisoformat(iso)
    return f"{SHORT_DAYS[d.weekday()]}, {d.day} {SHORT_MONTHS[d.month - 1]}"


def free_slots(iso, minutes, skip=None):
    """Свободные времена начала для процедуры длиной minutes на дату iso."""
    day = date.fromisoformat(iso)
    taken = []
    for b in load():
        if b["date"] == iso and b["status"] in BUSY and b["id"] != skip:
            s = start_dt(b)
            taken.append((s, s + timedelta(minutes=b["minutes"])))
    out, t = [], datetime(day.year, day.month, day.day, OPEN_H, tzinfo=TZ)
    end = datetime(day.year, day.month, day.day, CLOSE_H, tzinfo=TZ)
    while t + timedelta(minutes=minutes) <= end:
        e = t + timedelta(minutes=minutes)
        if t > now() + timedelta(minutes=30) and all(e <= s or t >= f for s, f in taken):
            out.append(t.strftime("%H:%M"))
        t += timedelta(minutes=STEP)
    return out


def open_days(minutes, skip=None, limit=DAYS_AHEAD):
    out = []
    for i in range(limit + 1):
        d = (now() + timedelta(days=i)).date()
        if d.weekday() in WORK_DAYS and free_slots(d.isoformat(), minutes, skip):
            out.append(d.isoformat())
    return out


def add_booking(iso, tm, svc, client, name, user_id, phone=None):
    with LOCK:
        title, minutes, price = SERVICES[svc]
        if tm not in free_slots(iso, minutes):
            return None
        items = load()
        b = {"id": max((x["id"] for x in items), default=0) + 1, "status": "new", "user_id": user_id,
             "client": client, "name": name, "date": iso, "time": tm, "service": title, "minutes": minutes,
             "price": price, "phone": phone, "created": now().isoformat(timespec="seconds"), "reminded": []}
        items.append(b)
        save(items)
        return b


def set_status(bid, status):
    with LOCK:
        items = load()
        b = find(items, bid)
        if not b:
            return None, "Запись не найдена"
        if status in BUSY and b["status"] not in BUSY and b["time"] not in free_slots(b["date"], b["minutes"], skip=bid):
            return None, "Это время уже занято другой записью"
        b["status"] = status
        save(items)
        return b, None


def move_booking(bid, iso, tm):
    with LOCK:
        items = load()
        b = find(items, bid)
        if not b or tm not in free_slots(iso, b["minutes"], skip=bid):
            return None
        b["was"] = when(b)
        b["date"], b["time"], b["status"], b["reminded"] = iso, tm, "moved", []
        save(items)
        return b


def card(b, note=""):
    phone = f"<b>{esc(b['phone'])}</b>" if b.get("phone") else "не указан (напишите клиенту в Telegram)"
    c = datetime.fromisoformat(b["created"])
    lines = [f"🌸 <b>Запись №{b['id']}</b> — {STATUS[b['status']]}", "", f"🗓 <b>{when(b)}</b>"]
    if b["status"] == "moved" and b.get("was"):
        lines.append(f"↪ Было: {b['was']}")
    lines += [f"💆 {esc(b['service'])} — {from_rub(b['price'])}", f"👤 Клиент: {b['client']}", f"📞 Телефон: {phone}",
              f"🕓 Оставлена: {c.day} {SHORT_MONTHS[c.month - 1]}, {c:%H:%M}"]
    if note:
        lines += ["", note]
    return "\n".join(lines)


def actions(b):
    rows = []
    if b["status"] != "confirmed":
        rows.append([btn("✅ Подтвердить" if b["status"] != "cancelled" else "↩ Вернуть и подтвердить", f"c:{b['id']}")])
    if b["status"] != "cancelled":
        rows.append([btn("🔁 Перенести", f"m:{b['id']}"), btn("❌ Отменить", f"x:{b['id']}")])
    return rows + [[btn("‹ К записям", "adm")]]


def to_owner(text, rows=None):
    if OWNER:
        tg("sendMessage", chat_id=OWNER, parse_mode="HTML", text=text, **({"reply_markup": kb(rows)} if rows else {}))


def send_to_owner(b):
    to_owner("<b>Новая запись</b>\n\n" + card(b), actions(b))


def notify_client(b, text):
    r = tg("sendMessage", chat_id=b["user_id"], parse_mode="HTML", text=text, reply_markup=kb([[btn("📅 Моя запись", "my")]]))
    return "📨 Клиент уведомлён." if r.get("ok") else "⚠️ Не удалось написать клиенту, свяжитесь с ним сами."


def upcoming():
    today = now().date().isoformat()
    return sorted((b for b in load() if b["status"] in BUSY and b["date"] >= today), key=lambda b: (b["date"], b["time"]))


def adm_view():
    items = upcoming()
    if not items:
        return "📋 <b>Записи</b>\n\nПредстоящих записей пока нет.", [[btn("🔄 Обновить", "adm")]]
    rows = [[btn(f"{STATUS[b['status']][0]} {day_short(b['date'])} {b['time']} · {b['name'][:20]}", f"v:{b['id']}")] for b in items[:15]]
    more = f"\nПоказаны ближайшие 15 из {len(items)}." if len(items) > 15 else ""
    return f"📋 <b>Записи</b> (предстоящих: {len(items)})\n\nНажмите на запись, чтобы подтвердить, перенести или отменить.{more}", rows + [[btn("🔄 Обновить", "adm")]]


def agenda_text():
    out = []
    for i, name in ((0, "Сегодня"), (1, "Завтра")):
        iso = (now() + timedelta(days=i)).date().isoformat()
        items = [b for b in upcoming() if b["date"] == iso]
        lines = [f"• <b>{b['time']}</b> — {esc(b['name'])}, {esc(b['service'])}" for b in items] or ["• записей нет"]
        out.append(f"<b>{name}, {day_label(iso)}</b>\n" + "\n".join(lines))
    return "📆 <b>Расписание</b>\n\n" + "\n\n".join(out)


def my_text(user_id):
    mine = [b for b in upcoming() if b.get("user_id") == user_id]
    if not mine:
        return "У вас пока нет предстоящих записей."
    out = []
    for b in mine[:5]:
        line = f"{STATUS[b['status']]}\n🗓 <b>{when(b)}</b>\n💆 {esc(b['service'])} — {from_rub(b['price'])}"
        if b["status"] == "moved" and b.get("was"):
            line += f"\n↪ Было: {b['was']}"
        out.append(line)
    return "<b>Ваши записи</b>\n\n" + "\n\n— — —\n\n".join(out)


def who(u):
    name = esc(" ".join(x for x in [u.get("first_name"), u.get("last_name")] if x) or "Клиент")
    return f'<a href="tg://user?id={u["id"]}">{name}</a>' + (f" (@{esc(u['username'])})" if u.get("username") else "")


# ---------- обработчики ----------
def on_message(m):
    global OWNER
    if m["chat"].get("type") != "private":
        return
    chat = m["chat"]["id"]
    t = (m.get("text") or "").strip()
    if t.startswith("/owner"):  # привязка владельца: /owner <код>
        if t.split()[-1:] == [OWNER_CODE] and len(t.split()) == 2:
            OWNER = chat
            kv_set("owner", chat)
            tg("sendMessage", chat_id=chat, text="✅ Готово! Все записи клиентов теперь приходят сюда.", reply_markup=menu(chat))
        else:
            tg("sendMessage", chat_id=chat, text="Неверный код.")
        return
    if m.get("contact"):
        with LOCK:
            items = load()
            mine = [b for b in items if b.get("user_id") == m["from"]["id"]]
            if mine:
                mine[-1]["phone"] = m["contact"]["phone_number"]
                save(items)
        to_owner(f"📞 Телефон клиента {who(m['from'])}: <b>{esc(m['contact']['phone_number'])}</b>")
        tg("sendMessage", chat_id=chat, text="Спасибо! Мы свяжемся с вами, если понадобится уточнить время.",
           reply_markup={"remove_keyboard": True})
        return
    tg("sendMessage", chat_id=chat, text=greeting(chat), reply_markup=menu(chat))


OWNER_KEYS = ("adm", "agenda", "v", "c", "m", "md", "mt", "x", "xx")


def on_owner(k, n, show):
    if k == "adm":
        text, rows = adm_view()
        show(text, rows + [BACK])
        return
    if k == "agenda":
        show(agenda_text(), [[btn("🔄 Обновить", "agenda")], BACK])
        return
    b = find(load(), n[0])
    if not b:
        show("Запись не найдена.", [[btn("‹ К записям", "adm")]])
        return
    if k == "v":
        show(card(b), actions(b))
    elif k == "c":
        b, err = set_status(n[0], "confirmed")
        if err:
            b = find(load(), n[0])
            show(card(b, "⚠️ " + err), actions(b))
            return
        note = notify_client(b, f"✅ <b>Ваша запись подтверждена!</b>\n\n🗓 {when(b)}\n💆 {esc(b['service'])}\n📍 {ADDRESS}\n\nЖдём вас! Напомним о визите заранее 🌸")
        show(card(b, note), actions(b))
    elif k == "m":
        days = [btn("📅 " + day_short(d), f"md:{b['id']}:{d.replace('-', '')}") for d in open_days(b["minutes"], b["id"])[:8]]
        show(f"🔁 <b>Перенос записи №{b['id']}</b>\nСейчас: {when(b)}\n\nВыберите новую дату:", chunk(days, 2) + [[btn("‹ Назад", f"v:{b['id']}")]])
    elif k == "md":
        iso = f"{str(n[1])[:4]}-{str(n[1])[4:6]}-{str(n[1])[6:]}"
        hours = free_slots(iso, b["minutes"], b["id"])
        if not hours:
            show("На эту дату свободного времени нет.", [[btn("‹ Назад", f"m:{b['id']}")]])
            return
        show(f"🔁 <b>Перенос записи №{b['id']}</b>\nНовая дата: {day_label(iso)}\n\nВыберите время:",
             chunk([btn(h, f"mt:{b['id']}:{n[1]}:{h.replace(':', '')}") for h in hours], 4) + [[btn("‹ Назад", f"m:{b['id']}")]])
    elif k == "mt":
        iso = f"{str(n[1])[:4]}-{str(n[1])[4:6]}-{str(n[1])[6:]}"
        tm = f"{str(n[2]).zfill(4)[:2]}:{str(n[2]).zfill(4)[2:]}"
        b2 = move_booking(b["id"], iso, tm)
        if not b2:
            show("Это время уже занято. Выберите другое.", [[btn("‹ Назад", f"m:{b['id']}")]])
            return
        note = notify_client(b2, f"🔁 <b>Ваша запись перенесена</b>\n\n↪ Было: {b2['was']}\n🗓 <b>Стало: {when(b2)}</b>\n\nЕсли новое время не подходит, напишите нам: {CONTACT_TG}")
        show(card(b2, note), actions(b2))
    elif k == "x":
        show(f"Отменить запись №{b['id']}?\n{when(b)}\nКлиент: {b['client']}\n\nВремя освободится для других клиентов.",
             [[btn("❌ Да, отменить", f"xx:{b['id']}")], [btn("‹ Нет, назад", f"v:{b['id']}")]])
    elif k == "xx":
        b, _ = set_status(n[0], "cancelled")
        note = notify_client(b, f"❌ <b>Ваша запись отменена</b>\n\n🗓 {when(b)}\n\nМожно выбрать другое время.")
        show(card(b, note), actions(b))


def on_callback(cb):
    chat, mid = cb["message"]["chat"]["id"], cb["message"]["message_id"]
    k, *a = cb["data"].split(":")
    tg("answerCallbackQuery", callback_query_id=cb["id"])

    def show(text, rows):
        tg("editMessageText", chat_id=chat, message_id=mid, text=text, parse_mode="HTML", reply_markup=kb(rows),
           disable_web_page_preview=True)

    if k == "menu":
        tg("editMessageText", chat_id=chat, message_id=mid, text=greeting(chat), reply_markup=menu(chat))
    elif k in OWNER_KEYS:
        if not OWNER or cb["from"]["id"] != OWNER:
            return
        on_owner(k, [int(x) for x in a], show)
    elif k == "my":
        mine = [b for b in upcoming() if b.get("user_id") == cb["from"]["id"] and b["status"] != "cancelled"]
        rows = [[btn(f"❌ Отменить {day_short(b['date'])} {b['time']}", f"cx:{b['id']}")] for b in mine[:5]]
        show(my_text(cb["from"]["id"]), rows + [BOOK, BACK])
    elif k == "cx":  # клиент отменяет сам
        b = find(load(), int(a[0]))
        if not b or b.get("user_id") != cb["from"]["id"]:
            return
        show(f"Отменить запись?\n\n🗓 {when(b)}\n💆 {esc(b['service'])}", [[btn("❌ Да, отменить", f"cxx:{b['id']}")], [btn("‹ Нет", "my")]])
    elif k == "cxx":
        b0 = find(load(), int(a[0]))
        if not b0 or b0.get("user_id") != cb["from"]["id"]:
            return
        b, _ = set_status(b0["id"], "cancelled")
        to_owner("<b>Клиент отменил запись</b>\n\n" + card(b))
        show("Запись отменена. Будем рады видеть вас в другое время 🌸", [BOOK, BACK])
    elif k == "price":
        show("<b>Услуги и цены</b>\n\n" + "\n".join(f"• {t} — <b>{from_rub(p)}</b>" for t, _, p in SERVICES)
             + "\n\nСтоимость зависит от опыта мастера: 35–52 ₽ за минуту. Точную сумму назовёт мастер, она считается по фактическому времени процедуры.\n\n🎁 Первая процедура может быть пробной, уточните при записи.", [BOOK, BACK])
    elif k == "about":
        show("<b>Электроэпиляция</b>\n\nМетод разрушает волосяную луковицу, не повреждая кожу. Это единственный способ избавиться "
             "от нежелательных волос навсегда.\n\nВ студии ведущий специалист с опытом более 15 лет "
             "и мастера студии. Сколько сеансов потребуется, подскажут на первой процедуре.", [BOOK, BACK])
    elif k == "place":
        show(f"<b>Как добраться</b>\n\n📍 {ADDRESS}\n🚏 Остановка «Проспект Калашникова», около 430 м пешком.\n🕗 Студия работает ежедневно, по предварительной записи.\n\n"
             f'<a href="{MAPS}">Открыть на Яндекс Картах</a>', [BOOK, BACK])
    elif k == "contact":
        show(f"<b>Связаться</b>\n\n📞 {PHONE}\n✈️ Telegram: {CONTACT_TG}\n📣 <a href=\"{CHANNEL}\">Канал студии</a> · <a href=\"{VK}\">ВКонтакте</a>\n\nИли запишитесь прямо здесь, это быстрее.", [BOOK, BACK])
    elif k == "b":
        show("Выберите процедуру:", [[btn(f"{t} — {from_rub(p)}", f"s:{i}")] for i, (t, _, p) in enumerate(SERVICES)] + [BACK])
    elif k == "s":
        i = int(a[0])
        days = [btn("📅 " + day_short(d), f"d:{i}:{d.replace('-', '')}") for d in open_days(SERVICES[i][1])[:10]]
        if not days:
            show("Свободных дат пока нет. Напишите нам: " + CONTACT_TG, [[btn("‹ Назад", "b")]])
            return
        show(f"<b>{SERVICES[i][0]}</b>\nВыберите дату:", chunk(days, 2) + [[btn("‹ Назад", "b")]])
    elif k == "d":
        i, ds = int(a[0]), a[1]
        iso = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
        hours = free_slots(iso, SERVICES[i][1])
        if not hours:
            show("На эту дату свободного времени не осталось. Выберите другую.", [[btn("‹ Назад", f"s:{i}")]])
            return
        show(f"<b>{SERVICES[i][0]}</b>\n{day_label(iso)}\n\nВо сколько удобно?",
             chunk([btn(h, f"t:{i}:{ds}:{h.replace(':', '')}") for h in hours], 4) + [[btn("‹ Назад", f"s:{i}")]])
    elif k == "t":
        i, ds, hm = int(a[0]), a[1], a[2]
        iso, tm = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}", f"{hm[:2]}:{hm[2:]}"
        show(f"<b>Проверьте запись</b>\n\n💆 {SERVICES[i][0]}\n🗓 {day_label(iso)}, {tm}\n💰 {from_rub(SERVICES[i][2])} (точную стоимость назовёт мастер)\n📍 {ADDRESS}",
             [[btn("✅ Записаться", f"ok:{i}:{ds}:{hm}")], [btn("↩ Начать заново", "b")]])
    elif k == "ok":
        i, ds, hm = int(a[0]), a[1], a[2]
        iso, tm = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}", f"{hm[:2]}:{hm[2:]}"
        u = cb["from"]
        b = add_booking(iso, tm, i, who(u), " ".join(x for x in [u.get("first_name"), u.get("last_name")] if x) or "Клиент", u["id"])
        if not b:
            show("😔 Это время только что заняли. Выберите другое.", [[btn("↩ Выбрать заново", f"s:{i}")]])
            return
        send_to_owner(b)
        show(f"✅ <b>Вы записаны!</b>\n\n🗓 {when(b)}\n💆 {esc(b['service'])}\n📍 {ADDRESS}\n\nМастер подтвердит запись, "
             "а перед визитом мы пришлём напоминание 🌸", [[btn("📅 Моя запись", "my")], BACK])
        tg("sendMessage", chat_id=chat, text="Чтобы нам было проще с вами связаться, можете поделиться номером 👇",
           reply_markup={"keyboard": [[{"text": "📞 Поделиться номером", "request_contact": True}]],
                         "resize_keyboard": True, "one_time_keyboard": True})


# ---------- напоминания ----------
def reminders_loop():
    while True:
        try:
            with LOCK:
                items, changed = load(), False
                for b in items:
                    if b["status"] not in ("new", "confirmed", "moved") or not b.get("user_id"):
                        continue
                    left = (start_dt(b) - now()).total_seconds() / 60
                    for mins, word in REMIND:
                        if 0 < left <= mins and mins not in b["reminded"]:
                            b["reminded"].append(mins)
                            changed = True
                            if left > mins - 90 or mins <= 120 and left > 20:  # не шлём напоминание «опоздавшее» на несколько часов
                                tg("sendMessage", chat_id=b["user_id"], parse_mode="HTML",
                                   text=f"🌸 <b>Напоминаем о записи {word}</b>\n\n🗓 {when(b)}\n💆 {esc(b['service'])}\n📍 {ADDRESS}",
                                   reply_markup=kb([[btn("📅 Моя запись", "my")]]))
                            break
                if changed:
                    save(items)
            # утренняя сводка владельцу в 8:00
            d = now()
            if OWNER and d.hour == 8 and kv_get("digest") != d.date().isoformat():
                kv_set("digest", d.date().isoformat())
                to_owner(agenda_text())
        except Exception as e:
            print("напоминания:", repr(e), flush=True)
        time.sleep(60)


class Health(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET


def setup_profile():
    for lang in ("", "ru"):
        tg("setMyDescription", description=DESCRIPTION, language_code=lang)
        tg("setMyShortDescription", short_description=SHORT_DESCRIPTION, language_code=lang)
        tg("setMyCommands", commands=[{"command": "start", "description": "Открыть меню"}], language_code=lang)
    tg("setChatMenuButton", menu_button={"type": "commands"})
    av = HERE / "avatar.jpg"
    if av.exists() and not kv_get("avatar") and not os.environ.get("SKIP_AVATAR"):
        r = tg_photo("setMyProfilePhoto", "file", av, photo=json.dumps({"type": "static", "photo": "attach://file"}))
        print("Аватарка:", "установлена" if r.get("ok") else f"не удалось через API ({r.get('description')}), поставьте через @BotFather → /setuserpic", flush=True)
        if r.get("ok"):
            kv_set("avatar", 1)


def main():
    if not TOKEN:
        raise SystemExit("Нет токена. Впишите BOT_TOKEN=... в файл .env рядом с bot.py")
    global OWNER
    if DATABASE_URL:
        neon("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    OWNER = OWNER or int(kv_get("owner", 0) or 0)
    me = tg("getMe")
    if not me.get("ok"):
        raise SystemExit("Не удалось подключиться к Telegram. Проверьте токен и интернет.")
    setup_profile()
    tg("deleteWebhook")
    threading.Thread(target=reminders_loop, daemon=True).start()
    port = os.environ.get("PORT")
    if port:  # на хостинге нужен веб-порт: по нему сервис проверяют и не даём ему «заснуть»
        threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", int(port)), Health).serve_forever, daemon=True).start()
    print(f"Бот @{me['result']['username']} запущен. Владелец: {OWNER or 'не привязан (напишите боту /owner ' + OWNER_CODE + ')'}. Остановить: Ctrl+C", flush=True)
    offset = None
    while True:
        r = tg("getUpdates", timeout=30, offset=offset, allowed_updates=["message", "callback_query"])
        if not r.get("ok"):
            time.sleep(3)
            continue
        for up in r["result"]:
            offset = up["update_id"] + 1
            try:
                if "callback_query" in up:
                    on_callback(up["callback_query"])
                elif "message" in up:
                    on_message(up["message"])
            except Exception as e:
                print("ошибка:", repr(e), flush=True)


if __name__ == "__main__":
    main()
