import asyncio
import hashlib
import os
import sqlite3
from datetime import datetime, timedelta
from aiohttp import web
from telethon import TelegramClient, events
from openai import OpenAI

# ===== Конфигурация =====
API_ID = int(os.environ['TG_API_ID'])
API_HASH = os.environ['TG_API_HASH']
BOT_TOKEN = os.environ['TG_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TG_CHANNEL_ID'])
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
NOTIFY_CHAT_ID = int(os.environ['NOTIFY_CHAT_ID'])

SOURCE_CHANNELS_DEFAULT = os.environ.get('SOURCE_CHANNELS', '@crypto_hd,@dimatkachevv')
KEYWORDS_DEFAULT = os.environ.get(
    'KEYWORDS',
    'биткоин,BTC,нефть,Сбер,Газпром,инфляция,ставка ЦБ,санкции,США,ЕС,крипта'
)
DEDUP_TTL_DAYS = int(os.environ.get('DEDUP_TTL_DAYS', '7'))
# ========================

openai_client = None
if OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

client = TelegramClient('bot_session', API_ID, API_HASH)
client.parse_mode = 'md'
is_paused = False
started_at = datetime.utcnow()
KEYWORDS = []
SOURCE_CHANNELS = []


# ===== Уведомления =====

async def notify(text: str):
    try:
        await client.send_message(NOTIFY_CHAT_ID, text, parse_mode='md')
    except Exception as e:
        print(f'[!] Ошибка уведомления: {e}')


# ===== База данных =====

def init_db():
    conn = sqlite3.connect('dedup.db')
    conn.execute('CREATE TABLE IF NOT EXISTS posted (hash TEXT PRIMARY KEY, posted_at TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS keywords (word TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS sources (channel TEXT PRIMARY KEY)')
    conn.commit()
    conn.close()


def load_keywords() -> list:
    conn = sqlite3.connect('dedup.db')
    rows = conn.execute('SELECT word FROM keywords').fetchall()
    conn.close()
    if rows:
        return [r[0] for r in rows]
    defaults = [kw.strip() for kw in KEYWORDS_DEFAULT.split(',')]
    _save_to_table('keywords', 'word', defaults)
    return defaults


def load_sources() -> list:
    conn = sqlite3.connect('dedup.db')
    rows = conn.execute('SELECT channel FROM sources').fetchall()
    conn.close()
    if rows:
        return [r[0] for r in rows]
    defaults = [ch.strip() for ch in SOURCE_CHANNELS_DEFAULT.split(',')]
    _save_to_table('sources', 'channel', defaults)
    return defaults


def _save_to_table(table: str, column: str, values: list):
    conn = sqlite3.connect('dedup.db')
    conn.execute(f'DELETE FROM {table}')
    for v in values:
        conn.execute(f'INSERT OR IGNORE INTO {table} ({column}) VALUES (?)', (v,))
    conn.commit()
    conn.close()


def save_keywords(words: list):
    _save_to_table('keywords', 'word', words)


def save_sources(channels: list):
    _save_to_table('sources', 'channel', channels)


def make_hash(text: str) -> str:
    return hashlib.md5(text.strip()[:300].encode('utf-8')).hexdigest()


def is_duplicate(text: str) -> bool:
    conn = sqlite3.connect('dedup.db')
    row = conn.execute('SELECT 1 FROM posted WHERE hash = ?', (make_hash(text),)).fetchone()
    conn.close()
    return row is not None


def mark_posted(text: str):
    conn = sqlite3.connect('dedup.db')
    conn.execute(
        'INSERT OR IGNORE INTO posted (hash, posted_at) VALUES (?, ?)',
        (make_hash(text), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def cleanup_old_records():
    cutoff = (datetime.utcnow() - timedelta(days=DEDUP_TTL_DAYS)).isoformat()
    conn = sqlite3.connect('dedup.db')
    deleted = conn.execute('DELETE FROM posted WHERE posted_at < ?', (cutoff,)).rowcount
    conn.commit()
    conn.close()
    if deleted:
        print(f'[~] Очищено старых записей: {deleted}')


def get_total_posted() -> int:
    conn = sqlite3.connect('dedup.db')
    count = conn.execute('SELECT COUNT(*) FROM posted').fetchone()[0]
    conn.close()
    return count


# ===== Обработчик новостей (регистрируется в main() после загрузки каналов) =====

async def news_handler(event):
    if is_paused:
        return
    msg = event.message
    if not msg or not msg.text:
        return

    for word in KEYWORDS:
        if word.lower() in msg.text.lower():
            if is_duplicate(msg.text):
                print(f'[~] Дубликат: {msg.text[:60]}')
                return
            print(f'[+] Новость по слову «{word}»: {msg.text[:60]}')
            try:
                new_text = await rewrite_with_ai(msg.text)

                photos = []
                if msg.photo:
                    fb = await client.download_media(msg.photo, file=bytes)
                    if fb:
                        photos.append(fb)
                elif msg.grouped_id:
                    try:
                        async for m in client.iter_messages(
                            event.chat_id, min_id=msg.id - 5, max_id=msg.id + 1
                        ):
                            if m.photo:
                                fb = await client.download_media(m.photo, file=bytes)
                                if fb:
                                    photos.append(fb)
                    except Exception as e:
                        print(f'[!] Ошибка альбома: {e}')

                if photos:
                    try:
                        await client.send_file(CHANNEL_ID, photos, caption=new_text)
                    except Exception:
                        await client.send_message(CHANNEL_ID, new_text)
                else:
                    await client.send_message(CHANNEL_ID, new_text)

                mark_posted(msg.text)
                cleanup_old_records()

                source = getattr(event.chat, 'username', None)
                await notify(
                    f'✅ *Опубликована новость*\n'
                    f'📌 {"@" + source if source else "канал"}\n'
                    f'🔑 `{word}`\n'
                    f'📝 `{msg.text[:100]}...`'
                )
            except Exception as e:
                print(f'[!] Ошибка публикации: {e}')
                await notify(f'❌ *Ошибка публикации*\n`{e}`')
            break


async def rewrite_with_ai(text: str) -> str:
    if not openai_client:
        return text
    try:
        r = openai_client.chat.completions.create(
            model='gpt-3.5-turbo',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'Ты — трейдер-аналитик. Перепиши новость коротко, по делу, '
                        'без воды, с цифрами. Добавь эмодзи (📈 📉 🔥 ⚠️ 💰). '
                        'Разбей на абзацы. В конце добавь хештеги #новости #трейдинг #крипто'
                    )
                },
                {'role': 'user', 'content': text}
            ],
            temperature=0.7
        )
        return r.choices[0].message.content
    except Exception as e:
        print(f'[!] Ошибка ИИ: {e}')
        await notify(f'⚠️ *Ошибка ИИ*\n`{e}`')
        return text


# ===== Команды управления =====

@client.on(events.NewMessage(pattern=r'^/(\w+)(?:\s+(.+))?$'))
async def command_handler(event):
    global is_paused, KEYWORDS, SOURCE_CHANNELS
    if event.sender_id != NOTIFY_CHAT_ID:
        return

    cmd = event.pattern_match.group(1).lower()
    arg = (event.pattern_match.group(2) or '').strip()

    SEP = '〰〰〰〰〰〰〰〰〰〰'

    # ── /status ──────────────────────────────────────────────
    if cmd == 'status':
        uptime = datetime.utcnow() - started_at
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        minutes = rem // 60
        state = '⏸ На паузе' if is_paused else '🟢 Работает'
        ai = '🟢 Включён' if openai_client else '🔴 Выключен'
        await event.respond(
            f'**📊 Статус бота**\n'
            f'{SEP}\n'
            f'🔘 Режим: {state}\n'
            f'⏱ Аптайм: **{hours}ч {minutes}мин**\n'
            f'📡 Слежу за каналами: **{len(SOURCE_CHANNELS)}**\n'
            f'🔑 Ключевых слов: **{len(KEYWORDS)}**\n'
            f'🤖 ИИ-переработка: {ai}\n'
            f'{SEP}'
        )

    # ── /stats ────────────────────────────────────────────────
    elif cmd == 'stats':
        total = get_total_posted()
        conn = sqlite3.connect('dedup.db')
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0).isoformat()
        week_start = (datetime.utcnow() - timedelta(days=7)).isoformat()
        today = conn.execute(
            'SELECT COUNT(*) FROM posted WHERE posted_at >= ?', (today_start,)
        ).fetchone()[0]
        week = conn.execute(
            'SELECT COUNT(*) FROM posted WHERE posted_at >= ?', (week_start,)
        ).fetchone()[0]
        conn.close()
        await event.respond(
            f'**📈 Статистика публикаций**\n'
            f'{SEP}\n'
            f'📅 Сегодня — **{today}** новостей\n'
            f'📆 За 7 дней — **{week}** новостей\n'
            f'📦 Всего в базе — **{total}** записей\n'
            f'{SEP}'
        )

    # ── /pause / /resume ──────────────────────────────────────
    elif cmd == 'pause':
        if is_paused:
            await event.respond(
                '⏸ **Бот уже на паузе**\n\n'
                'Чтобы возобновить работу — нажми /resume'
            )
        else:
            is_paused = True
            await event.respond(
                '⏸ **Бот поставлен на паузу**\n\n'
                'Новые посты публиковаться не будут.\n'
                'Возобновить: /resume'
            )

    elif cmd == 'resume':
        if not is_paused:
            await event.respond(
                '🟢 **Бот уже работает**\n\n'
                'Всё в порядке — слежу за каналами.'
            )
        else:
            is_paused = False
            await event.respond(
                '▶️ **Бот возобновлён!**\n\n'
                'Снова слежу за каналами и публикую новости.'
            )

    # ── /keywords ─────────────────────────────────────────────
    elif cmd == 'keywords':
        kw_list = '\n'.join(f'  {i+1}. {k}' for i, k in enumerate(KEYWORDS))
        await event.respond(
            f'**🔑 Ключевые слова** — {len(KEYWORDS)} шт.\n'
            f'{SEP}\n'
            f'{kw_list}\n'
            f'{SEP}\n'
            f'➕ Добавить: /add\_keyword слово\n'
            f'➖ Удалить: /remove\_keyword слово'
        )

    elif cmd == 'add_keyword':
        if not arg:
            await event.respond(
                '❌ **Не указано слово**\n\n'
                'Пример использования:\n'
                '/add\_keyword золото'
            )
            return
        if arg.lower() in [k.lower() for k in KEYWORDS]:
            await event.respond(
                f'⚠️ **Слово уже отслеживается**\n\n'
                f'`{arg}` есть в списке.\n'
                f'Посмотреть все слова: /keywords'
            )
            return
        KEYWORDS.append(arg)
        save_keywords(KEYWORDS)
        await event.respond(
            f'✅ **Слово добавлено!**\n\n'
            f'Теперь отслеживаю: `{arg}`\n'
            f'Всего слов в списке: **{len(KEYWORDS)}**'
        )
        print(f'[+] Ключевое слово: {arg}')

    elif cmd == 'remove_keyword':
        if not arg:
            await event.respond(
                '❌ **Не указано слово**\n\n'
                'Пример использования:\n'
                '/remove\_keyword золото'
            )
            return
        match = next((k for k in KEYWORDS if k.lower() == arg.lower()), None)
        if not match:
            await event.respond(
                f'⚠️ **Слово не найдено**\n\n'
                f'`{arg}` нет в списке.\n'
                f'Посмотреть список: /keywords'
            )
            return
        if len(KEYWORDS) <= 1:
            await event.respond(
                '❌ **Нельзя удалить последнее слово**\n\n'
                'В списке должно быть хотя бы одно слово.'
            )
            return
        KEYWORDS.remove(match)
        save_keywords(KEYWORDS)
        await event.respond(
            f'🗑 **Слово удалено**\n\n'
            f'`{match}` убрано из отслеживания.\n'
            f'Осталось слов: **{len(KEYWORDS)}**'
        )
        print(f'[-] Ключевое слово удалено: {match}')

    # ── /sources ──────────────────────────────────────────────
    elif cmd == 'sources':
        src_list = '\n'.join(f'  {i+1}. {s}' for i, s in enumerate(SOURCE_CHANNELS))
        await event.respond(
            f'**📡 Каналы-источники** — {len(SOURCE_CHANNELS)} шт.\n'
            f'{SEP}\n'
            f'{src_list}\n'
            f'{SEP}\n'
            f'➕ Добавить: /add\_source @channel\n'
            f'➖ Удалить: /remove\_source @channel\n'
            f'🔄 Применить изменения: /restart\_sources'
        )

    elif cmd == 'add_source':
        if not arg:
            await event.respond(
                '❌ **Не указан канал**\n\n'
                'Пример использования:\n'
                '/add\_source @rbc\_news'
            )
            return
        ch = arg if arg.startswith('@') else f'@{arg}'
        if ch.lower() in [s.lower() for s in SOURCE_CHANNELS]:
            await event.respond(
                f'⚠️ **Канал уже добавлен**\n\n'
                f'`{ch}` уже есть в списке источников.'
            )
            return
        SOURCE_CHANNELS.append(ch)
        save_sources(SOURCE_CHANNELS)
        await event.respond(
            f'✅ **Канал добавлен!**\n\n'
            f'`{ch}` добавлен в источники.\n'
            f'Всего каналов: **{len(SOURCE_CHANNELS)}**\n\n'
            f'Чтобы бот начал его читать — /restart\_sources'
        )

    elif cmd == 'remove_source':
        if not arg:
            await event.respond(
                '❌ **Не указан канал**\n\n'
                'Пример использования:\n'
                '/remove\_source @rbc\_news'
            )
            return
        ch = arg if arg.startswith('@') else f'@{arg}'
        match = next((s for s in SOURCE_CHANNELS if s.lower() == ch.lower()), None)
        if not match:
            await event.respond(
                f'⚠️ **Канал не найден**\n\n'
                f'`{ch}` нет в списке.\n'
                f'Посмотреть список: /sources'
            )
            return
        if len(SOURCE_CHANNELS) <= 1:
            await event.respond(
                '❌ **Нельзя удалить последний канал**\n\n'
                'Должен остаться хотя бы один источник.'
            )
            return
        SOURCE_CHANNELS.remove(match)
        save_sources(SOURCE_CHANNELS)
        await event.respond(
            f'🗑 **Канал удалён**\n\n'
            f'`{match}` убран из источников.\n'
            f'Осталось каналов: **{len(SOURCE_CHANNELS)}**\n\n'
            f'Чтобы изменения вступили в силу — /restart\_sources'
        )

    elif cmd == 'restart_sources':
        client.remove_event_handler(news_handler)
        client.add_event_handler(news_handler, events.NewMessage(chats=SOURCE_CHANNELS))
        src_list = '\n'.join(f'  ✔ {s}' for s in SOURCE_CHANNELS)
        await event.respond(
            f'🔄 **Подписки обновлены!**\n'
            f'{SEP}\n'
            f'{src_list}\n'
            f'{SEP}\n'
            f'Слежу за **{len(SOURCE_CHANNELS)}** каналами.'
        )
        print(f'[~] Перезапущены источники: {SOURCE_CHANNELS}')

    # ── /test ─────────────────────────────────────────────────
    elif cmd == 'test':
        lines = [f'**🔍 Проверка каналов**\n{SEP}']
        for ch in SOURCE_CHANNELS:
            try:
                entity = await client.get_entity(ch)
                title = getattr(entity, 'title', ch)
                members = getattr(entity, 'participants_count', None)
                sub = f' · {members:,} подписчиков' if members else ''
                lines.append(f'✅ **{title}**\n    `{ch}`{sub}')
            except Exception as e:
                lines.append(f'❌ `{ch}` — недоступен')
        state_str = '⏸ На паузе' if is_paused else '🟢 Работает'
        lines.append(f'{SEP}\n🔑 Слов: **{len(KEYWORDS)}** · Режим: {state_str}')
        await event.respond('\n\n'.join(lines))

    # ── /help ─────────────────────────────────────────────────
    elif cmd == 'help':
        await event.respond(
            f'**🤖 Команды бота**\n'
            f'{SEP}\n\n'
            f'**📊 Мониторинг**\n'
            f'/status — состояние и аптайм\n'
            f'/stats — статистика публикаций\n'
            f'/test — проверить каналы\n\n'
            f'**⏯ Управление**\n'
            f'/pause — поставить на паузу\n'
            f'/resume — возобновить работу\n\n'
            f'**🔑 Ключевые слова**\n'
            f'/keywords — показать список\n'
            f'/add\_keyword слово — добавить\n'
            f'/remove\_keyword слово — удалить\n\n'
            f'**📡 Каналы-источники**\n'
            f'/sources — показать список\n'
            f'/add\_source @channel — добавить\n'
            f'/remove\_source @channel — удалить\n'
            f'/restart\_sources — применить изменения\n'
            f'{SEP}'
        )

    else:
        await event.respond(
            '❓ **Неизвестная команда**\n\n'
            'Напиши /help — покажу всё что умею.'
        )


# ===== Веб-сервер (для Render.com) =====

async def health(request):
    return web.Response(text='OK')


async def start_web():
    app = web.Application()
    app.router.add_get('/', health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    await web.TCPSite(runner, '0.0.0.0', port).start()
    print(f'[*] Веб-сервер запущен на порту {port}')


# ===== Запуск =====

async def main():
    global KEYWORDS, SOURCE_CHANNELS
    init_db()
    KEYWORDS = load_keywords()
    SOURCE_CHANNELS = load_sources()

    # Регистрируем обработчик новостей ПОСЛЕ загрузки списка каналов
    client.add_event_handler(news_handler, events.NewMessage(chats=SOURCE_CHANNELS))

    await start_web()

    print(f'[*] Каналы ({len(SOURCE_CHANNELS)}): {SOURCE_CHANNELS}')
    print(f'[*] Ключевые слова ({len(KEYWORDS)}): {KEYWORDS}')
    print(f'[*] ИИ: {"включён" if openai_client else "выключен"}')

    await client.start(bot_token=BOT_TOKEN)
    print('[✓] Бот запущен! Жду новости...')

    await notify(
        '🤖 *Бот запущен*\n\n'
        f'📡 Каналов: {len(SOURCE_CHANNELS)}\n'
        f'🔑 Ключевых слов: {len(KEYWORDS)}\n'
        f'🤖 ИИ: {"включён" if openai_client else "выключен"}\n\n'
        'Напиши /help для списка команд'
    )

    await client.run_until_disconnected()


if __name__ == '__main__':
    # Бот запускается ТОЛЬКО на Render.com
    # На Replit он только хранится и редактируется
    if not os.environ.get('RENDER'):
        print('=' * 50)
        print('  Это Replit — редактор кода.')
        print('  Бот работает на Render.com.')
        print('  Запускать здесь не нужно.')
        print('=' * 50)

        async def _dummy():
            app = web.Application()
            app.router.add_get('/', health)
            runner = web.AppRunner(app)
            await runner.setup()
            port = int(os.environ.get('PORT', 8080))
            await web.TCPSite(runner, '0.0.0.0', port).start()
            print(f'[*] Заглушка на порту {port}')
            await asyncio.sleep(3600 * 24 * 365)

        asyncio.run(_dummy())
    else:
        asyncio.run(main())
