import asyncio
import hashlib
import os
import sqlite3
from datetime import datetime, timedelta
from aiohttp import web
from telethon import TelegramClient, events
from openai import OpenAI

API_ID = int(os.environ['TG_API_ID'])
API_HASH = os.environ['TG_API_HASH']
BOT_TOKEN = os.environ['TG_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TG_CHANNEL_ID'])
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
NOTIFY_CHAT_ID = int(os.environ['NOTIFY_CHAT_ID'])

SOURCE_CHANNELS_RAW = os.environ.get('SOURCE_CHANNELS', '@crypto_hd,@dimatkachevv')
SOURCE_CHANNELS = [ch.strip() for ch in SOURCE_CHANNELS_RAW.split(',')]

KEYWORDS_RAW = os.environ.get('KEYWORDS', 'биткоин,BTC,нефть,Сбер,Газпром,инфляция,ставка ЦБ,санкции,США,ЕС,крипта')
KEYWORDS = [kw.strip() for kw in KEYWORDS_RAW.split(',')]

DEDUP_TTL_DAYS = int(os.environ.get('DEDUP_TTL_DAYS', '7'))

openai_client = None
if OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

client = TelegramClient('bot_session', API_ID, API_HASH)
is_paused = False
started_at = datetime.utcnow()

async def notify(text: str):
    try:
        await client.send_message(NOTIFY_CHAT_ID, text, parse_mode='md')
    except Exception as e:
        print(f'[!] Ошибка уведомления: {e}')

def init_db():
    conn = sqlite3.connect('dedup.db')
    conn.execute('CREATE TABLE IF NOT EXISTS posted (hash TEXT PRIMARY KEY, posted_at TEXT NOT NULL)')
    conn.commit()
    conn.close()

def make_hash(text):
    return hashlib.md5(text.strip()[:300].encode('utf-8')).hexdigest()

def is_duplicate(text):
    conn = sqlite3.connect('dedup.db')
    row = conn.execute('SELECT 1 FROM posted WHERE hash = ?', (make_hash(text),)).fetchone()
    conn.close()
    return row is not None

def mark_posted(text):
    conn = sqlite3.connect('dedup.db')
    conn.execute('INSERT OR IGNORE INTO posted (hash, posted_at) VALUES (?, ?)', (make_hash(text), datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def cleanup_old_records():
    cutoff = (datetime.utcnow() - timedelta(days=DEDUP_TTL_DAYS)).isoformat()
    conn = sqlite3.connect('dedup.db')
    conn.execute('DELETE FROM posted WHERE posted_at < ?', (cutoff,))
    conn.commit()
    conn.close()

def get_total_posted():
    conn = sqlite3.connect('dedup.db')
    count = conn.execute('SELECT COUNT(*) FROM posted').fetchone()[0]
    conn.close()
    return count

@client.on(events.NewMessage(pattern=r'^/(\w+)'))
async def command_handler(event):
    global is_paused
    if event.sender_id != NOTIFY_CHAT_ID:
        return
    cmd = event.pattern_match.group(1).lower()
    if cmd == 'status':
        uptime = datetime.utcnow() - started_at
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes = remainder // 60
        state = '⏸ На паузе' if is_paused else '✅ Работает'
        await event.respond(f'📊 *Статус бота*\n\nСостояние: {state}\n⏱ Аптайм: {hours}ч {minutes}мин\n📡 Каналы: {", ".join(SOURCE_CHANNELS)}\n🔑 Ключевых слов: {len(KEYWORDS)}\n🤖 ИИ: {"включён" if openai_client else "выключен"}', parse_mode='md')
    elif cmd == 'stats':
        total = get_total_posted()
        conn = sqlite3.connect('dedup.db')
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0).isoformat()
        today = conn.execute('SELECT COUNT(*) FROM posted WHERE posted_at >= ?', (today_start,)).fetchone()[0]
        conn.close()
        await event.respond(f'📈 *Статистика публикаций*\n\n📅 Сегодня: *{today}*\n📦 Всего за {DEDUP_TTL_DAYS} дней: *{total}*', parse_mode='md')
    elif cmd == 'pause':
        if is_paused:
            await event.respond('⏸ Бот уже на паузе. /resume — возобновить')
        else:
            is_paused = True
            await event.respond('⏸ *Бот на паузе.* /resume — возобновить', parse_mode='md')
    elif cmd == 'resume':
        if not is_paused:
            await event.respond('✅ Бот уже работает.')
        else:
            is_paused = False
            await event.respond('✅ *Бот возобновлён!*', parse_mode='md')
    elif cmd == 'keywords':
        await event.respond(f'🔑 *Ключевые слова:*\n' + '\n'.join(f'• {k}' for k in KEYWORDS), parse_mode='md')
    elif cmd == 'help':
        await event.respond('🤖 *Команды:*\n\n/status — состояние\n/stats — статистика\n/pause — пауза\n/resume — возобновить\n/keywords — ключевые слова\n/help — справка', parse_mode='md')
    else:
        await event.respond('❓ Неизвестная команда. /help')

@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def handler(event):
    global is_paused
    if is_paused:
        return
    msg = event.message
    if not msg or not msg.text:
        return
    for word in KEYWORDS:
        if word.lower() in msg.text.lower():
            if is_duplicate(msg.text):
                return
            print(f'[+] Новость: {msg.text[:60]}')
            try:
                new_text = msg.text
                if openai_client:
                    try:
                        r = openai_client.chat.completions.create(model='gpt-3.5-turbo', messages=[{'role':'system','content':'Ты — трейдер-аналитик. Перепиши новость коротко, по делу, без воды, с цифрами. Добавь эмодзи (📈 📉 🔥 ⚠️ 💰). В конце добавь хештеги #новости #трейдинг #крипто'},{'role':'user','content':msg.text}], temperature=0.7)
                        new_text = r.choices[0].message.content
                    except Exception as e:
                        await notify(f'⚠️ *Ошибка ИИ*\n`{e}`')
                photos = []
                if msg.photo:
                    fb = await client.download_media(msg.photo, file=bytes)
                    if fb: photos.append(fb)
                elif msg.grouped_id:
                    async for m in client.iter_messages(event.chat_id, min_id=msg.id-5, max_id=msg.id+1):
                        if m.photo:
                            fb = await client.download_media(m.photo, file=bytes)
                            if fb: photos.append(fb)
                if photos:
                    try:
                        await client.send_file(CHANNEL_ID, photos, caption=new_text)
                    except:
                        await client.send_message(CHANNEL_ID, new_text)
                else:
                    await client.send_message(CHANNEL_ID, new_text)
                mark_posted(msg.text)
                cleanup_old_records()
                source = getattr(event.chat, 'username', None)
                await notify(f'✅ *Опубликована новость*\n📌 {"@"+source if source else "канал"}\n🔑 `{word}`\n📝 `{msg.text[:100]}...`')
            except Exception as e:
                await notify(f'❌ *Ошибка*\n`{e}`')
            break

async def health(request):
    return web.Response(text='OK')

async def start_web():
    app = web.Application()
    app.router.add_get('/', health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    await web.TCPSite(runner, '0.0.0.0', port).start()
    print(f'[*] Веб-сервер на порту {port}')

async def main():
    init_db()
    await start_web()
    await client.start(bot_token=BOT_TOKEN)
    print('[✓] Бот запущен!')
    await notify(f'🤖 *Бот запущен*\n📡 Слежу за: {", ".join(SOURCE_CHANNELS)}\n🔑 Ключевых слов: {len(KEYWORDS)}\n\nНапиши /help для списка команд')
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
