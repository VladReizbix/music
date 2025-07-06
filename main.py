import os
import re
import asyncio
import logging
import random
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardButton, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import yt_dlp
from cryptography.fernet import Fernet
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

def get_encryption_key():
    key = os.getenv('ENCRYPTION_KEY')
    if not key:
        try:
            with open('encryption_key.key', 'rb') as key_file:
                key = key_file.read()
            logger.info("Ключ шифрования загружен из encryption_key.key")
        except FileNotFoundError:
            key = Fernet.generate_key()
            with open('encryption_key.key', 'wb') as key_file:
                key_file.write(key)
            logger.info("Сгенерирован новый ключ шифрования и сохранён в encryption_key.key")
    else:
        key = key.encode()
    return key

def decrypt_token(encrypted_token, key):
    try:
        cipher = Fernet(key)
        return cipher.decrypt(encrypted_token).decode()
    except Exception as e:
        logger.error(f"Ошибка при дешифровании токена: {e}")
        raise

ENCRYPTION_KEY = get_encryption_key()
cipher = Fernet(ENCRYPTION_KEY)

try:
    with open('encrypted_token.bin', 'rb') as token_file:
        ENCRYPTED_TOKEN = token_file.read()
    logger.info("Зашифрованный токен загружен из encrypted_token.bin")
except FileNotFoundError:
    logger.warning("Файл encrypted_token.bin не найден. Создаём новый зашифрованный токен.")
    with open('encrypted_token.bin', 'wb') as token_file:
        token_file.write(ENCRYPTED_TOKEN)
    logger.info("Зашифрованный токен сохранён в encrypted_token.bin")

API_TOKEN = decrypt_token(ENCRYPTED_TOKEN, ENCRYPTION_KEY)

MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_RESULTS = 20
AD_ACTION_THRESHOLD = 20
PREMIUM_DAYS = {"30": 30, "90": 90, "365": 365}
PREMIUM_PRICES = {"30": 199, "90": 499, "365": 2299}
NEW_USER_DISCOUNT = 0.20
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '7026603143').split(',')]
CHANNEL_ID = int(os.getenv('CHANNEL_ID', '-1002715409948'))
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME', '@SoundPlus1')
PAYMENT_CHANNEL_ID = int(os.getenv('PAYMENT_CHANNEL_ID', '-1002747322675'))

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()

conn = sqlite3.connect('music_bot_youtube.db')
cursor = conn.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        balance INTEGER DEFAULT 0,
        referrals INTEGER DEFAULT 0,
        lang TEXT DEFAULT 'Русский',
        premium_until TEXT,
        downloads_today INTEGER DEFAULT 0,
        total_downloads INTEGER DEFAULT 0,
        last_reset TEXT DEFAULT '',
        action_count INTEGER DEFAULT 0,
        is_new_user BOOLEAN DEFAULT TRUE,
        referrer_id INTEGER DEFAULT NULL
    )
""")

# Migrate existing database to add total_downloads column
try:
    cursor.execute("ALTER TABLE users ADD COLUMN total_downloads INTEGER DEFAULT 0")
    conn.commit()
    logger.info("Added total_downloads column to users table")
except sqlite3.OperationalError:
    logger.info("total_downloads column already exists")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        days INTEGER,
        screenshot TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        processed_at TEXT
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS history (
        user_id INTEGER,
        title TEXT,
        artist TEXT,
        duration TEXT,
        video_id TEXT,
        created_at TEXT,
        source TEXT
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS favorites (
        user_id INTEGER,
        title TEXT,
        artist TEXT,
        duration TEXT,
        video_id TEXT,
        created_at TEXT,
        source TEXT
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_status (
        id INTEGER PRIMARY KEY,
        is_disabled BOOLEAN DEFAULT FALSE,
        disabled_until TEXT
    )
""")

cursor.execute("INSERT OR IGNORE INTO bot_status (id, is_disabled) VALUES (1, FALSE)")
conn.commit()

class SearchStates(StatesGroup):
    searching = State()

class PaymentStates(StatesGroup):
    waiting_for_screenshot = State()

class AdminStates(StatesGroup):
    waiting_for_ad = State()
    waiting_for_button_title = State()
    waiting_for_button_url = State()
    waiting_for_premium_user = State()
    waiting_for_premium_days = State()
    waiting_for_disable_duration = State()

def get_user(uid):
    cursor.execute("SELECT * FROM users WHERE id = ?", (uid,))
    user = cursor.fetchone()
    if not user:
        cursor.execute("INSERT INTO users (id) VALUES (?)", (uid,))
        conn.commit()
    return True

def update_user(uid, **kwargs):
    keys, values = zip(*kwargs.items())
    fields = ', '.join(f"{k} = ?" for k in keys)
    cursor.execute(f"UPDATE users SET {fields} WHERE id = ?", (*values, uid))
    conn.commit()

def get_user_field(uid, field):
    cursor.execute(f"SELECT {field} FROM users WHERE id = ?", (uid,))
    res = cursor.fetchone()
    return res[0] if res else None

def has_premium(uid):
    p = get_user_field(uid, 'premium_until')
    if not p:
        return False
    try:
        return datetime.fromisoformat(p) > datetime.utcnow()
    except ValueError:
        return False

def can_download(uid):
    if has_premium(uid):
        return True
    total_downloads = get_user_field(uid, 'total_downloads') or 0
    return total_downloads < 30  # Free users limited to 30 tracks

def should_send_ad(uid):
    if has_premium(uid):
        return False
    action_count = get_user_field(uid, 'action_count') or 0
    return action_count >= AD_ACTION_THRESHOLD

def reset_action_count(uid):
    update_user(uid, action_count=0)

def increment_action_count(uid):
    current_count = get_user_field(uid, 'action_count') or 0
    update_user(uid, action_count=current_count + 1)

def send_ad_text():
    ads = [
        FSInputFile("1.mp4"),
        FSInputFile("2.mp4"),
        FSInputFile("3.mp4"),
        FSInputFile("4.mp4")
    ]
    return random.choice(ads)

def format_duration(duration):
    try:
        duration = int(duration)
        if duration <= 0:
            return "??:??"
        return f"{duration // 60}:{duration % 60:02d}"
    except (TypeError, ValueError):
        return "??:??"

def sanitize_filename(name: str) -> str:
    if not isinstance(name, str):
        name = str(name)
    return re.sub(r'[^\w\-_\. ]', '_', name)[:100]

def youtube_match_filter(info, *, incomplete):
    title = info.get('title', '').lower()
    duration = info.get('duration')
    if duration and duration > 600:  
        return "Слишком длинное видео"
    if any(keyword in title for keyword in ['live', 'stream', 'video', 'стрим', 'видео', 'прямой эфир']):
        return "Исключено: видео, стрим или не музыка"
    return None

async def youtube_search(query: str, max_results=20, max_duration=600, exclude_playlists=True, is_fallback=False):
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'format': 'bestaudio/best',
        'default_search': f'ytsearch{max_results}',
        'nocheckcertificate': True,
        'extract_flat': 'in_playlist',
        'match_filter': youtube_match_filter,
        'noplaylist': exclude_playlists
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_query = f"{query} официальный аудио музыка" if not is_fallback else f"{query} русские хиты музыка"
            info = ydl.extract_info(search_query, download=False)
            entries = info.get('entries', [])
            results = []
            for e in entries:
                vid = e.get('id')
                title = e.get('title', 'Unknown Title')
                duration = e.get('duration')
                artist = e.get('uploader', 'Unknown Artist')
                if not vid or not title: 
                    logger.warning(f"Skipping invalid entry: {e}")
                    continue
                duration_str = format_duration(duration)
                results.append({
                    'video_id': vid,
                    'title': title,
                    'artist': artist,
                    'duration': duration_str
                })
            logger.info(f"Found {len(results)} tracks for query: {query} (fallback: {is_fallback})")
            
            if not results and not is_fallback:
                logger.info(f"No results for query: {query}, trying fallback search")
                return await youtube_search(query, max_results, max_duration, exclude_playlists, is_fallback=True)
            return results
    except Exception as e:
        logger.error(f"Error searching YouTube for query '{query}': {str(e)}")
        return []

async def download_audio(video_id, title=None):
    if not os.path.exists('temp'):
        os.makedirs('temp')
    filename = sanitize_filename(title or video_id)
    output_file = f"temp/{filename}.webm"
    if os.path.exists(output_file):
        return output_file
    ydl_opts = {
        'format': 'bestaudio',
        'outtmpl': f"temp/{filename}.%(ext)s",
        'quiet': True,
        'nocheckcertificate': true,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            url = f"https://www.youtube.com/watch?v={video_id}"
            ydl.download([url])
        if os.path.getsize(output_file) > MAX_FILE_SIZE:
            os.remove(output_file)
            return None
        return output_file
    except Exception as e:
        logger.error(f"Error downloading audio: {e}")
        return None

def enable_bot():
    cursor.execute("UPDATE bot_status SET is_disabled = FALSE, disabled_until = NULL WHERE id = 1")
    conn.commit()

def log_history(uid, item, source='youtube'):
    if not all(key in item for key in ['title', 'artist', 'duration', 'video_id']):
        logger.error("Invalid item format for history logging")
        return
    cursor.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uid, item['title'], item['artist'], item['duration'], item['video_id'], datetime.utcnow().isoformat(), source)
    )
    cursor.execute(
        """
        DELETE FROM history
        WHERE user_id = ? AND rowid NOT IN
        (SELECT rowid FROM history WHERE user_id = ? ORDER BY datetime(created_at) DESC LIMIT 50)
        """, (uid, uid))
    conn.commit()

def is_admin(uid):
    return uid in ADMIN_IDS

def is_bot_disabled():
    cursor.execute("SELECT is_disabled, disabled_until FROM bot_status WHERE id = 1")
    status = cursor.fetchone()
    if not status:
        return False
    is_disabled, disabled_until = status
    if is_disabled and disabled_until:
        try:
            if datetime.fromisoformat(disabled_until) > datetime.utcnow():
                return True
            else:
                enable_bot()
                return False
        except ValueError:
            return False
    return is_disabled

def disable_bot(minutes):
    until = datetime.utcnow() + timedelta(minutes=minutes)
    cursor.execute(
        "UPDATE bot_status SET is_disabled = TRUE, disabled_until = ? WHERE id = 1",
        (until.isoformat(),)
    )
    conn.commit()

async def broadcast_message(message_text, users=None, photo_path=None, video_path=None, button_title=None, button_url=None):
    if users is None:
        cursor.execute("SELECT id FROM users")
        users = [row[0] for row in cursor.fetchall()]
    count = 0
    kb = None
    if button_title and button_url:
        kb = InlineKeyboardBuilder()
        kb.add(InlineKeyboardButton(text=button_title, url=button_url))
    for user_id in users:
        try:
            if photo_path:
                photo = FSInputFile(photo_path)
                await bot.send_photo(user_id, photo, caption=message_text, parse_mode='HTML',
                                    reply_markup=kb.as_markup() if kb else None)
            elif video_path:
                video = FSInputFile(video_path)
                await bot.send_video(user_id, video, caption=message_text, parse_mode='HTML',
                                    reply_markup=kb.as_markup() if kb else None)
            else:
                await bot.send_message(user_id, message_text, parse_mode='HTML',
                                      reply_markup=kb.as_markup() if kb else None)
            count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to send message to {user_id}: {e}")
    return count

async def get_bot_stats():
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE premium_until > ?", (datetime.utcnow().isoformat(),))
    premium_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM history")
    total_downloads = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE referrals > 0")
    users_with_referrals = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM payments WHERE status = 'pending'")
    pending_payments = cursor.fetchone()[0]
    return {
        'total_users': total_users,
        'premium_users': premium_users,
        'total_downloads': total_downloads,
        'users_with_referrals': users_with_referrals,
        'pending_payments': pending_payments
    }

def get_premium_price(days, is_new_user=False):
    base_price = PREMIUM_PRICES.get(str(days), 0)
    if is_new_user and days == 30:
        return int(base_price * (1 - NEW_USER_DISCOUNT))
    return base_price

def update_referral_balance(referrer_id, referral_id, amount):
    if referrer_id:
        percent_bonus = int(amount * 0.05)
        premium_bonus = 50 if get_user_field(referral_id, 'premium_until') else 0
        total_bonus = percent_bonus + premium_bonus
        if total_bonus > 0:
            current_balance = get_user_field(referrer_id, 'balance') or 0
            update_user(referrer_id, balance=current_balance + total_bonus)
            cursor.execute("UPDATE users SET referrals = referrals + 1 WHERE id = ?", (referrer_id,))
            conn.commit()

def create_payment(user_id, amount, days):
    created_at = datetime.utcnow().isoformat()
    cursor.execute(
        "INSERT INTO payments (user_id, amount, days, created_at) VALUES (?, ?, ?, ?)",
        (user_id, amount, days, created_at)
    )
    conn.commit()
    return cursor.lastrowid

def get_payment(payment_id):
    cursor.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
    return cursor.fetchone()

def update_payment(payment_id, status, screenshot=None):
    processed_at = datetime.utcnow().isoformat()
    if screenshot:
        cursor.execute(
            "UPDATE payments SET status = ?, processed_at = ?, screenshot = ? WHERE id = ?",
            (status, processed_at, screenshot, payment_id)
        )
    else:
        cursor.execute(
            "UPDATE payments SET status = ?, processed_at = ? WHERE id = ?",
            (status, processed_at, payment_id)
        )
    conn.commit()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if is_bot_disabled():
        await message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return

    uid = message.from_user.id
    get_user(uid)

    if not await check_subscription(uid):
        kb = InlineKeyboardBuilder()
        kb.add(InlineKeyboardButton(text="📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME[1:]}"))
        kb.add(InlineKeyboardButton(text="🔄 Я подписался", callback_data="check_sub_start"))
        await message.answer(
            "📢 Для использования бота необходимо подписаться на наш канал!",
            reply_markup=kb.as_markup()
        )
        return

    referral_id = None
    command_text = message.text
    if command_text and " " in command_text:
        args = command_text.split()[1:]
        for arg in args:
            if arg.startswith("ref="):
                try:
                    referral_id = int(arg.split("ref=")[1])
                    if referral_id == uid:
                        referral_id = None
                    break
                except ValueError:
                    pass

    if referral_id and get_user_field(uid, 'is_new_user'):
        update_user(uid, referrer_id=referral_id)
        update_user(referral_id, referrals=get_user_field(referral_id, 'referrals') + 1)
        await bot.send_message(
            referral_id,
            f"🎉 Новый реферал! Пользователь {uid} присоединился по вашей ссылке. "
            f"Вы получите бонус, когда он купит премиум."
        )

    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="📋 Профиль", callback_data="profile"))
    if is_admin(uid):
        kb.add(InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel"))

    welcome_message = """
    🤖 *Как пользоваться ботом SoundPlus?* Очень просто и удобно!

    🔎 *Ищи треки по:*
    ✔️ Названию.
    ✔️ Названию исполнителя.

    🎶 *Открывай свою волну* 🌊:
    🏆 Топовые хиты
    🆕 И Свежие новинки
    🔥 Музыкальный вайб дня

    🎧 *Слушай где угодно.*
    А самое главное *бесплатно* 🆓

    💡 *Мы в SoundPlus помогаем быстро находить и слушать любимую музыку без лишних хлопот!*
    """
    is_new_user = get_user_field(uid, 'is_new_user')
    if has_premium(uid):
        await message.answer("Привет! Премиум тариф активен 🎵", reply_markup=kb.as_markup())
    else:
        total_downloads = get_user_field(uid, 'total_downloads') or 0
        discount_msg = "\n✨ Новым пользователям: первый месяц премиума со скидкой 20%!" if is_new_user else ""
        welcome_message += f"\n📢 Бесплатный тариф: осталось {30 - total_downloads} треков из 30."
        await message.answer(
            f"Привет! Бесплатный тариф активирован 🎵{discount_msg}\n👉 Приглашай друзей и зарабатывай бонусы!",
            reply_markup=kb.as_markup())

    try:
        mp4 = FSInputFile("welcome.mp4")
        await message.answer_animation(
            mp4,
            caption=welcome_message,
            reply_markup=kb.as_markup(),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error sending welcome animation: {e}")
        await message.answer(welcome_message, reply_markup=kb.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "profile")
async def profile(cb: types.CallbackQuery):
    if is_bot_disabled():
        await cb.message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    user_id = cb.from_user.id
    get_user(user_id)
    increment_action_count(user_id)

    balance = get_user_field(user_id, 'balance') or 0
    referrals = get_user_field(user_id, 'referrals') or 0
    lang = get_user_field(user_id, 'lang') or 'Русский'
    premium_until = get_user_field(user_id, 'premium_until')
    referrer_id = get_user_field(user_id, 'referrer_id')

    text = (
        f"👤 <b>Профиль</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💰 Баланс: <b>{balance}₽</b>\n"
        f"👥 Рефералы: <b>{referrals}</b>\n"
        f"🌐 Язык: <b>{lang}</b>\n"
        f"💎 Premium: <b>{'✅ Активен' if has_premium(user_id) else '❌ Нет'}</b>\n"
        f"📌 Ваша ссылка:\n"
        f"<code>https://t.me/SoundPlus_bot?start=ref={user_id}</code>\n\n"
        f"📢 Дайте друзьям эту ссылку, и вы оба получите вознаграждение!"
    )

    if referrer_id:
        text += f"👤 Пригласил вас: <code>{referrer_id}</code>\n"

    if has_premium(user_id) and premium_until:
        until_date = datetime.fromisoformat(premium_until).strftime('%d.%m.%Y')
        text += f"⏳ Подписка до: <b>{until_date}</b>\n"

    inline_kb = InlineKeyboardBuilder()
    inline_kb.add(InlineKeyboardButton(text="📅 Купить премиум", callback_data="buy"))

    reply_kb = ReplyKeyboardBuilder()
    reply_kb.add(types.KeyboardButton(text="👤 Профиль"))
    reply_kb.add(types.KeyboardButton(text="🕘 История"))
    reply_kb.add(types.KeyboardButton(text="⭐ Избранное"))
    reply_kb.add(types.KeyboardButton(text="🔍 Поиск музыки"))
    reply_kb.add(types.KeyboardButton(text="🆕 Новинки"))
    reply_kb.add(types.KeyboardButton(text="🏆 Топ песен"))
    reply_kb.add(types.KeyboardButton(text="🌊 Моя волна"))
    reply_kb.adjust(2)

    await cb.message.answer(text, reply_markup=inline_kb.as_markup(), parse_mode="HTML")
    await cb.message.answer("Выберите действие:", reply_markup=reply_kb.as_markup(resize_keyboard=True))

    if should_send_ad(user_id):
        await cb.message.answer(send_ad_text())
        reset_action_count(user_id)

@dp.callback_query(F.data == "buy")
async def buy(cb: types.CallbackQuery):
    if is_bot_disabled():
        await cb.message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    uid = cb.from_user.id
    get_user(uid)
    increment_action_count(uid)
    is_new_user = get_user_field(uid, 'is_new_user')

    kb = InlineKeyboardBuilder()
    for k in PREMIUM_DAYS:
        days = int(k)
        price = get_premium_price(days, is_new_user)
        discount_text = " (-20%)" if is_new_user and days == 30 else ""
        savings_text = " (Экономия 16%)" if days == 90 else " (Экономия 50%)" if days == 365 else ""
        kb.add(InlineKeyboardButton(text=f"💳 {days // 30} мес премиума - {price} RUB{savings_text}{discount_text}",
                                    callback_data=f"prem_{k}"))
    kb.adjust(1)
    await cb.message.answer("Выберите срок премиума:", reply_markup=kb.as_markup())

    if should_send_ad(uid):
        await cb.message.answer(send_ad_text())
        reset_action_count(uid)

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: types.CallbackQuery):
    uid = cb.from_user.id
    if not is_admin(uid):
        await cb.answer("❌ Доступ запрещён: вы не администратор", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📢 Отправить рекламу", callback_data="admin_send_ad"))
    kb.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
        InlineKeyboardButton(text="💎 Выдать премиум", callback_data="admin_grant_premium")
    )
    kb.row(
        InlineKeyboardButton(text="🔴 Отключить бота", callback_data="admin_disable_bot"),
        InlineKeyboardButton(text="🟢 Включить бота", callback_data="admin_enable_bot")
    )
    kb.row(InlineKeyboardButton(text="📑 Проверить платежи", callback_data="admin_review_payments"))
    await cb.message.answer("🛠 Админ-панель:", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "check_sub_start")
async def check_subscription_start(cb: types.CallbackQuery):
    uid = cb.from_user.id
    if not await check_subscription(uid):
        await cb.answer("❌ Вы все еще не подписаны на канал", show_alert=True)
        return
    await cb.message.delete()
    await cmd_start(cb.message)

async def check_subscription(user_id: int) -> bool:
    try:
        try:
            chat_member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
            return chat_member.status in ['member', 'administrator', 'creator']
        except Exception as e:
            logger.warning(f"Error checking subscription by ID: {e}")
            try:
                chat_member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
                return chat_member.status in ['member', 'administrator', 'creator']
            except Exception as e:
                logger.warning(f"Error checking subscription by username: {e}")
                return False
    except Exception as e:
        logger.error(f"Fatal error in check_subscription: {e}")
        return False

@dp.message(F.text.in_({"🔍 Поиск музыки", "🆕 Новинки", "🏆 Топ песен", "🌊 Моя волна"}))
async def check_subscription_wrapper(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if not await check_subscription(uid):
        kb = InlineKeyboardBuilder()
        kb.add(InlineKeyboardButton(text="📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME[1:]}"))
        kb.add(InlineKeyboardButton(text="🔄 Я подписался", callback_data=f"check_sub_{message.text}"))
        await message.answer(
            "📢 Для использования этой функции необходимо подписаться на наш канал!",
            reply_markup=kb.as_markup()
        )
        return
    increment_action_count(uid)
    if message.text == "🔍 Поиск музыки":
        await cmd_search(message, state)
    elif message.text == "🆕 Новинки":
        await cmd_new_releases(message, state)
    elif message.text == "🏆 Топ песен":
        await cmd_top_songs(message, state)
    elif message.text == "🌊 Моя волна":
        await cmd_my_wave(message, state)
    if should_send_ad(uid):
        await message.answer(send_ad_text())
        reset_action_count(uid)

@dp.callback_query(lambda c: c.data.startswith("check_sub_"))
async def check_subscription_other(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if not await check_subscription(uid):
        await cb.answer("❌ Вы все еще не подписаны на канал", show_alert=True)
        return
    command = cb.data.split("_", 2)[-1]
    increment_action_count(uid)
    if command == "🔍 Поиск музыки":
        await cmd_search(cb.message, state)
    elif command == "🆕 Новинки":
        await cmd_new_releases(cb.message, state)
    elif command == "🏆 Топ песен":
        await cmd_top_songs(cb.message, state)
    elif command == "🌊 Моя волна":
        await cmd_my_wave(cb.message, state)
    await cb.message.delete()
    if should_send_ad(uid):
        await cb.message.answer(send_ad_text())
        reset_action_count(uid)

@dp.callback_query(lambda c: c.data.startswith("prem_"))
async def buy_premium(cb: types.CallbackQuery, state: FSMContext):
    if is_bot_disabled():
        await cb.message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    try:
        days = int(cb.data.split('_')[1])
        uid = cb.from_user.id
        get_user(uid)
        increment_action_count(uid)
        is_new_user = get_user_field(uid, 'is_new_user')
        price = get_premium_price(days, is_new_user)

        if not await check_subscription(uid):
            kb = InlineKeyboardBuilder()
            kb.add(InlineKeyboardButton(text="📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME[1:]}"))
            kb.add(InlineKeyboardButton(text="🔄 Проверить подписку", callback_data=f"check_sub_prem_{days}"))
            await cb.message.answer(
                "📢 Для продолжения необходимо подписаться на наш канал @SoundPlus1",
                reply_markup=kb.as_markup()
            )
            return

        payment_id = create_payment(uid, price, days)
        payment_message = (
            f"🎵 *Оплата премиум-подписки SoundPlus*\n\n"
            f"📌 <b>Сумма к оплате:</b> {price} RUB\n"
            f"📌 <b>Срок подписки:</b> {days} дней\n\n"
            f"💳 <b>Реквизиты для оплаты:</b>\n"
            f"Карта: <code>2200 7012 0139 3961</code>\n"
            f"Получатель: Михаил.С\n\n"
            f"📌 <b>Ваш ID платежа:</b> <code>{payment_id}</code>\n\n"
            f"1. Переведите точную сумму {price} RUB на указанную карту\n"
            f"2. Нажмите кнопку 'Отправить чек' и прикрепите скриншот перевода\n\n"
            f"После проверки платежа премиум будет активирован в течение 2 минут до 5 часов.\n"
            f"Если возникли проблемы, пишите @SoundPlusSupport"
        )
        kb = InlineKeyboardBuilder()
        kb.add(InlineKeyboardButton(text="📤 Отправить чек", callback_data=f"send_receipt_{payment_id}"))
        kb.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_buy"))
        kb.adjust(1)
        await cb.message.answer(payment_message, parse_mode="HTML", reply_markup=kb.as_markup())
        if should_send_ad(uid):
            await cb.message.answer(send_ad_text())
            reset_action_count(uid)
    except Exception as e:
        logger.error(f"Error in buy_premium: {e}")
        await cb.answer("❌ Произошла ошибка при оформлении платежа", show_alert=True)

@dp.callback_query(lambda c: c.data.startswith("send_receipt_"))
async def prompt_receipt(cb: types.CallbackQuery, state: FSMContext):
    payment_id = int(cb.data.split('_')[-1])
    await state.set_state(PaymentStates.waiting_for_screenshot)
    await state.update_data(payment_id=payment_id)
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_buy"))
    await cb.message.answer("📤 Пожалуйста, отправьте скриншот чека об оплате:", reply_markup=kb.as_markup())

@dp.message(PaymentStates.waiting_for_screenshot, F.photo)
async def process_payment_screenshot(message: types.Message, state: FSMContext):
    if is_bot_disabled():
        await message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    data = await state.get_data()
    payment_id = data.get('payment_id')
    if not payment_id:
        await message.answer("❌ Ошибка: не найден ID платежа")
        await state.clear()
        return
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    file_path = file_info.file_path
    screenshot_path = f"temp/payment_{payment_id}.jpg"
    await bot.download_file(file_path, screenshot_path)
    update_payment(payment_id, "pending", screenshot_path)
    payment = get_payment(payment_id)
    if payment:
        user_id = payment[1]
        amount = payment[2]
        days = payment[3]
        caption = (
            f"🆔 Платеж: <code>{payment_id}</code>\n"
            f"👤 Пользователь: <code>{user_id}</code>\n"
            f"💰 Сумма: <b>{amount} RUB</b>\n"
            f"📅 Дней: <b>{days}</b>\n\n"
            f"Проверьте платеж и подтвердите или отклоните."
        )
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_pay_{payment_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_pay_{payment_id}")
        )
        try:
            with open(screenshot_path, 'rb') as photo_file:
                await bot.send_photo(
                    PAYMENT_CHANNEL_ID,
                    types.BufferedInputFile(photo_file.read(), filename=f"payment_{payment_id}.jpg"),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb.as_markup()
                )
        except Exception as e:
            logger.error(f"Error sending payment to channel: {e}")
    await message.answer(
        "✅ Скриншот платежа получен и отправлен на проверку. "
        "Премиум будет активирован после подтверждения платежа администратором."
    )
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("confirm_pay_"))
async def confirm_payment(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
    payment_id = int(cb.data.split('_')[-1])
    payment = get_payment(payment_id)
    if not payment:
        await cb.answer("❌ Платеж не найден", show_alert=True)
        return
    user_id = payment[1]
    amount = payment[2]
    days = payment[3]
    until = datetime.utcnow() + timedelta(days=days)
    update_user(user_id, premium_until=until.isoformat(), is_new_user=False)
    update_payment(payment_id, "completed")
    referrer_id = get_user_field(user_id, 'referrer_id')
    if referrer_id:
        update_referral_balance(referrer_id, user_id, amount)
        await bot.send_message(
            referrer_id,
            f"🎉 Ваш реферал купил премиум!\n"
            f"💰 Вы получили бонус: {int(amount * 0.05)} RUB"
        )
    await cb.message.edit_caption(
        f"✅ Платеж <code>{payment_id}</code> подтвержден администратором @{cb.from_user.username}\n"
        f"👤 Пользователь: <code>{user_id}</code>\n"
        f"💎 Премиум активирован до {until.strftime('%d.%m.%Y')}",
        parse_mode="HTML"
    )
    await bot.send_message(
        user_id,
        f"🎉 Ваш платеж подтвержден! Премиум активирован на {days} дней.\n"
        f"💎 Срок действия: до {until.strftime('%d.%m.%Y')}\n\n"
        f"Спасибо за покупку! Если возникнут вопросы, пишите @SoundPlusSupport"
    )

@dp.callback_query(lambda c: c.data.startswith("reject_pay_"))
async def reject_payment(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
    payment_id = int(cb.data.split('_')[-1])
    payment = get_payment(payment_id)
    if not payment:
        await cb.answer("❌ Платеж не найден", show_alert=True)
        return
    user_id = payment[1]
    update_payment(payment_id, "rejected")
    await cb.message.edit_caption(
        f"❌ Платеж <code>{payment_id}</code> отклонен администратором @{cb.from_user.username}\n"
        f"👤 Пользователь: <code>{user_id}</code>",
        parse_mode="HTML"
    )
    await bot.send_message(
        user_id,
        f"❌ Ваш платеж №{payment_id} отклонен администратором.\n"
        f"Возможные причины:\n"
        f"- Неверная сумма\n"
        f"- Нечитаемый скриншот\n"
        f"- Подозрение в мошенничестве\n\n"
        f"Если вы считаете это ошибкой, свяжитесь с @SoundPlusSupport"
    )

@dp.callback_query(lambda c: c.data.startswith("check_sub_prem_"))
async def check_subscription_premium(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    days = int(cb.data.split('_')[-1])
    if not await check_subscription(uid):
        await cb.answer("❌ Вы все еще не подписаны на канал", show_alert=True)
        return
    await cb.message.delete()
    await buy_premium(cb, state)

@dp.callback_query(F.data == "back_to_buy")
async def back_to_buy(cb: types.CallbackQuery):
    uid = cb.from_user.id
    get_user(uid)
    increment_action_count(uid)
    is_new_user = get_user_field(uid, 'is_new_user')
    kb = InlineKeyboardBuilder()
    for k in PREMIUM_DAYS:
        days = int(k)
        price = get_premium_price(days, is_new_user)
        savings_text = " (Экономия 16%)" if days == 90 else " (Экономия 50%)" if days == 365 else ""
        discount_text = " (-20%)" if is_new_user and days == 30 else ""
        kb.add(InlineKeyboardButton(text=f"💳 {days // 30} мес премиума - {price} RUB{savings_text}{discount_text}",
                                    callback_data=f"prem_{k}"))
    kb.adjust(1)
    await cb.message.answer("Выберите срок премиума:", reply_markup=kb.as_markup())
    if should_send_ad(uid):
        await cb.message.answer(send_ad_text())
        reset_action_count(uid)

@dp.callback_query(F.data == "admin_review_payments")
async def admin_review_payments(cb: types.CallbackQuery):
    uid = cb.from_user.id
    if not is_admin(uid):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
    cursor.execute("SELECT id, user_id, days, amount FROM payments WHERE status = 'pending'")
    payments = cursor.fetchall()
    if not payments:
        await cb.message.answer("📑 Нет ожидающих проверки платежей.")
        return
    text = "📑 Ожидающие проверки платежи:\n"
    kb = InlineKeyboardBuilder()
    for payment_id, user_id, days, amount in payments:
        text += f"ID платежа: {payment_id}, Пользователь: {user_id}, {days} дней, {amount} RUB\n"
        kb.add(InlineKeyboardButton(text=f"Платёж {payment_id}", callback_data=f"review_payment_{payment_id}"))
    kb.adjust(1)
    await cb.message.answer(text, reply_markup=kb.as_markup())

@dp.callback_query(lambda c: c.data.startswith("review_payment_"))
async def review_payment(cb: types.CallbackQuery):
    uid = cb.from_user.id
    if not is_admin(uid):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
    try:
        payment_id = int(cb.data.split('_')[-1])
        cursor.execute("SELECT user_id, days, amount, screenshot FROM payments WHERE id = ?", (payment_id,))
        payment = cursor.fetchone()
        if not payment:
            await cb.message.answer("❌ Платёж не найден")
            return
        user_id, days, amount, screenshot_path = payment
        kb = InlineKeyboardBuilder()
        kb.add(InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_pay_{payment_id}"))
        kb.add(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_pay_{payment_id}"))
        if screenshot_path and os.path.exists(screenshot_path):
            if screenshot_path.endswith(('.jpg', '.jpeg', '.png')):
                receipt = FSInputFile(screenshot_path)
                await cb.message.answer_photo(
                    receipt,
                    caption=f"Чек от пользователя {user_id} на {days} дней ({amount} RUB)",
                    reply_markup=kb.as_markup()
                )
            else:
                receipt = FSInputFile(screenshot_path)
                await cb.message.answer_document(
                    receipt,
                    caption=f"Чек от пользователя {user_id} на {days} дней ({amount} RUB)",
                    reply_markup=kb.as_markup()
                )
        else:
            await cb.message.answer(
                f"Чек от пользователя {user_id} на {days} дней ({amount} RUB)",
                reply_markup=kb.as_markup()
            )
    except Exception as e:
        logger.error(f"Error reviewing payment: {e}")
        await cb.message.answer("❌ Ошибка при просмотре платежа")

@dp.callback_query(F.data == "admin_enable_bot")
async def admin_enable_bot(cb: types.CallbackQuery):
    uid = cb.from_user.id
    if not is_admin(uid):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
    if not is_bot_disabled():
        await cb.message.answer("✅ Бот уже активен")
        return
    enable_bot()
    await cb.message.answer("✅ Бот включен")

@dp.callback_query(F.data == "admin_send_ad")
async def admin_send_ad(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if not is_admin(uid):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
    await cb.message.answer("Введите текст рекламного сообщения или отправьте фото/видео с подписью:")
    await state.set_state(AdminStates.waiting_for_ad)

@dp.message(AdminStates.waiting_for_ad)
async def process_ad_message(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("❌ Доступ запрещён")
        await state.clear()
        return
    ad_text = message.text or message.caption or ""
    photo_path = None
    video_path = None
    if message.photo:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        file_path = file_info.file_path
        photo_path = f"temp/ad_photo_{uid}.jpg"
        await bot.download_file(file_path, photo_path)
        ad_text = message.caption or "Реклама"
    elif message.video:
        video = message.video
        file_info = await bot.get_file(video.file_id)
        file_path = file_info.file_path
        video_path = f"temp/ad_video_{uid}.mp4"
        await bot.download_file(file_path, video_path)
        ad_text = message.caption or "Реклама"
    elif not ad_text:
        await message.answer("❌ Текст или подпись не могут быть пустыми")
        return
    await state.update_data(ad_text=ad_text, photo_path=photo_path, video_path=video_path)
    await message.answer(
        "Хотите добавить кнопку к рекламе? Если да, введите текст кнопки (например, 'Перейти'). Если нет, отправьте 'Нет':")
    await state.set_state(AdminStates.waiting_for_button_title)

@dp.message(AdminStates.waiting_for_button_title)
async def process_button_title(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("❌ Доступ запрещён")
        await state.clear()
        return
    button_title = message.text.strip()
    if button_title.lower() == 'нет':
        data = await state.get_data()
        count = await broadcast_message(
            data['ad_text'],
            photo_path=data.get('photo_path'),
            video_path=data.get('video_path')
        )
        await message.answer(f"✅ Реклама отправлена {count} пользователям")
        if data.get('photo_path') and os.path.exists(data['photo_path']):
            os.remove(data['photo_path'])
        if data.get('video_path') and os.path.exists(data['video_path']):
            os.remove(data['video_path'])
        await state.clear()
        return
    await state.update_data(button_title=button_title)
    await message.answer("Введите URL для кнопки (например, https://t.me/your_channel):")
    await state.set_state(AdminStates.waiting_for_button_url)

@dp.message(AdminStates.waiting_for_button_url)
async def process_button_url(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("❌ Доступ запрещён")
        await state.clear()
        return
    button_url = message.text.strip()
    if not re.match(r'^https?://', button_url):
        await message.answer("❌ Неверный формат URL. Введите корректный URL, начинающийся с http:// или https://")
        return
    data = await state.get_data()
    count = await broadcast_message(
        data['ad_text'],
        photo_path=data.get('photo_path'),
        video_path=data.get('video_path'),
        button_title=data['button_title'],
        button_url=button_url
    )
    await message.answer(f"✅ Реклама с кнопкой отправлена {count} пользователям")
    if data.get('photo_path') and os.path.exists(data['photo_path']):
        os.remove(data['photo_path'])
    if data.get('video_path') and os.path.exists(data['video_path']):
        os.remove(data['video_path'])
    await state.clear()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: types.CallbackQuery):
    uid = cb.from_user.id
    if not is_admin(uid):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
    stats = await get_bot_stats()
    text = (
        f"📊 <b>Статистика бота</b>\n"
        f"👥 Всего пользователей: <b>{stats['total_users']}</b>\n"
        f"💎 Премиум-пользователи: <b>{stats['premium_users']}</b>\n"
        f"🎵 Всего скачиваний: <b>{stats['total_downloads']}</b>\n"
        f"👤 Пользователи с рефералами: <b>{stats['users_with_referrals']}</b>\n"
        f"📑 Ожидающие платежи: <b>{stats['pending_payments']}</b>\n"
    )
    await cb.message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "admin_grant_premium")
async def admin_grant_premium(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if not is_admin(uid):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
    await cb.message.answer("Введите ID пользователя для выдачи премиума:")
    await state.set_state(AdminStates.waiting_for_premium_user)

@dp.message(AdminStates.waiting_for_premium_user)
async def process_premium_user(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("❌ Доступ запрещён")
        await state.clear()
        return
    try:
        user_id = int(message.text.strip())
        cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        if not cursor.fetchone():
            await message.answer("❌ Пользователь не найден")
            await state.clear()
            return
        await state.update_data(premium_user_id=user_id)
        kb = InlineKeyboardBuilder()
        for days in PREMIUM_DAYS.values():
            kb.add(InlineKeyboardButton(text=f"{days // 30} месяцев", callback_data=f"admin_prem_days_{days}"))
        kb.adjust(1)
        await message.answer("Выберите срок премиума:", reply_markup=kb.as_markup())
        await state.set_state(AdminStates.waiting_for_premium_days)
    except ValueError:
        await message.answer("❌ Неверный формат ID")
        await state.clear()

@dp.callback_query(lambda c: c.data.startswith("admin_prem_days_"))
async def process_premium_days(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if not is_admin(uid):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
    try:
        days = int(cb.data.split('_')[-1])
        data = await state.get_data()
        user_id = data.get('premium_user_id')
        if not user_id:
            await cb.message.answer("❌ Ошибка: ID пользователя не найден")
            await state.clear()
            return
        until = datetime.utcnow() + timedelta(days=days)
        update_user(user_id, premium_until=until.isoformat(), is_new_user=False)
        await cb.message.answer(f"✅ Пользователю {user_id} выдан премиум на {days // 30} месяцев")
        await bot.send_message(user_id, f"🎉 Вам выдан премиум на {days // 30} месяцев!")
        await state.clear()
    except Exception as e:
        logger.error(f"Error granting premium: {e}")
        await cb.message.answer("❌ Ошибка при выдаче премиума")
        await state.clear()

@dp.callback_query(F.data == "admin_disable_bot")
async def admin_disable_bot(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if not is_admin(uid):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
    if is_bot_disabled():
        cursor.execute("SELECT disabled_until FROM bot_status WHERE id = 1")
        disabled_until = cursor.fetchone()[0]
        until = datetime.fromisoformat(disabled_until).strftime('%d.%m.%Y %H:%M:%S UTC')
        await cb.message.answer(f"⚠️ Бот уже отключен до {until}")
        return
    await cb.message.answer("Введите количество минут, на которое нужно отключить бота (например, 30):")
    await state.set_state(AdminStates.waiting_for_disable_duration)

@dp.message(AdminStates.waiting_for_disable_duration)
async def process_disable_duration(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("❌ Доступ запрещён")
        await state.clear()
        return
    try:
        minutes = int(message.text.strip())
        if minutes <= 0:
            await message.answer("❌ Введите положительное число минут")
            return
        disable_bot(minutes)
        until = (datetime.utcnow() + timedelta(minutes=minutes)).strftime('%d.%m.%Y %H:%M:%S UTC')
        await message.answer(f"✅ Бот отключен на {minutes} минут. Будет активен после {until}")
        await state.clear()
    except ValueError:
        await message.answer("❌ Неверный формат. Введите число минут")

@dp.message(F.text == "👤 Профиль")
async def cmd_profile(message: types.Message):
    if is_bot_disabled():
        await message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    user_id = message.from_user.id
    get_user(user_id)
    increment_action_count(user_id)
    balance = get_user_field(user_id, 'balance') or 0
    referrals = get_user_field(user_id, 'referrals') or 0
    lang = get_user_field(user_id, 'lang') or 'Русский'
    premium_until = get_user_field(user_id, 'premium_until')
    text = (
        f"👤 <b>Профиль</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💰 Баланс: <b>{balance}₽</b>\n"
        f"👥 Рефералы: <b>{referrals}</b>\n"
        f"🌐 Язык: <b>{lang}</b>\n"
        f"💎 Premium: <b>{'✅ Активен' if has_premium(user_id) else '❌ Нет'}</b>\n"
        f"📌 Ваша ссылка:\n"
        f"<code>https://t.me/SoundPlus_bot?start=ref={user_id}</code>\n\n"
        f"📢 Дайте друзьям эту ссылку, и вы оба получите вознаграждение!"
    )
    if has_premium(user_id) and premium_until:
        until_date = datetime.fromisoformat(premium_until).strftime('%d.%m.%Y')
        text += f"⏳ Подписка до: <b>{until_date}</b>\n"
    inline_kb = InlineKeyboardBuilder()
    inline_kb.add(InlineKeyboardButton(text="📅 Купить премиум", callback_data="buy"))
    await message.answer(text, reply_markup=inline_kb.as_markup(), parse_mode="HTML")
    if should_send_ad(user_id):
        await message.answer(send_ad_text())
        reset_action_count(user_id)

@dp.callback_query(F.data == "search")
async def prompt_search(cb: types.CallbackQuery, state: FSMContext):
    if is_bot_disabled():
        await cb.message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    uid = cb.from_user.id
    increment_action_count(uid)
    await cb.message.answer("🔍 Введите название или исполнителя для поиска на YouTube:")
    await state.set_state(SearchStates.searching)
    if should_send_ad(uid):
        await cb.message.answer(send_ad_text())
        reset_action_count(uid)

@dp.message(SearchStates.searching)
async def process_search(message: types.Message, state: FSMContext):
    if is_bot_disabled():
        await message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    uid = message.from_user.id
    get_user(uid)
    increment_action_count(uid)
    query = message.text.strip()
    results = await youtube_search(query)
    await state.clear()
    if not results:
        await message.answer("❌ Ничего не найдено по запросу.")
        return
    await state.update_data(search_results=results)
    await render_search_results(message, results)
    if should_send_ad(uid):
        await message.answer(send_ad_text())
        reset_action_count(uid)

async def render_search_results(message: types.Message, results, page=0):
    ITEMS_PER_PAGE = 5
    total = len(results)
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_results = results[start:end]
    kb = InlineKeyboardBuilder()
    for item in page_results:
        kb.row(InlineKeyboardButton(
            text=f"{item['title']} [{item['duration']}]",
            callback_data=f"select_{item['video_id']}"
        ))
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton(text="⏮ Назад", callback_data=f"page_{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="Дальше ⏭", callback_data=f"page_{page + 1}"))
    if nav:
        kb.row(*nav)
    await message.answer("Результаты поиска:", reply_markup=kb.as_markup())

@dp.callback_query(lambda c: c.data.startswith("page_"))
async def cb_pagination(callback: types.CallbackQuery, state: FSMContext):
    if is_bot_disabled():
        await callback.message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    uid = callback.from_user.id
    increment_action_count(uid)
    page = int(callback.data.split("_")[1])
    query_data = await state.get_data()
    results = query_data.get("search_results")
    if not results:
        await callback.message.answer("Результаты поиска не найдены.")
        return
    await render_search_results(callback.message, results, page)
    await callback.answer()
    if should_send_ad(uid):
        await callback.message.answer(send_ad_text())
        reset_action_count(uid)

@dp.callback_query(lambda c: c.data.startswith("select_"))
async def cb_select_track(callback: types.CallbackQuery):
    if is_bot_disabled():
        await callback.message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    uid = callback.from_user.id
    increment_action_count(uid)
    video_id = callback.data[len("select_"):]
    ydl_opts = {'quiet': True, 'nocheckcertificate': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        title = info.get('title', 'Unknown')
        duration = info.get('duration')
        duration_str = format_duration(duration)
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="▶️ Прослушать", callback_data=f"play_{video_id}"))
    kb.add(InlineKeyboardButton(text="⭐ Добавить в избранное", callback_data=f"fav_{video_id}"))
    await callback.message.answer(
        f"Выбран трек:\n<b>{title}</b> [{duration_str}]",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    if should_send_ad(uid):
        await callback.message.answer(send_ad_text())
        reset_action_count(uid)

@dp.callback_query(lambda c: c.data.startswith("play_"))
async def cb_play(callback: types.CallbackQuery):
    if is_bot_disabled():
        await callback.message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    video_id = callback.data[len("play_"):]
    uid = callback.from_user.id
    increment_action_count(uid)
    if not can_download(uid):
        await callback.message.answer(
            "❌ Вы достигли лимита в 30 треков для бесплатного тарифа. Купите премиум для неограниченного доступа!"
        )
        return
    try:
        ydl_opts = {'quiet': True, 'nocheckcertificate': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            title = info.get('title', 'Unknown')
            duration = info.get('duration')
            duration_str = format_duration(duration)
            artist = info.get('uploader', 'Unknown Artist')
        audio_path = await download_audio(video_id, title=title)
        if not audio_path:
            await callback.message.answer("❌ Ошибка: файл слишком большой или не удалось скачать.")
            return
        audio = FSInputFile(audio_path)
        await callback.message.answer_audio(audio)
        log_history(uid, {
            'video_id': video_id,
            'title': title,
            'artist': artist,
            'duration': duration_str
        })
        if not has_premium(uid):
            current_total = get_user_field(uid, 'total_downloads') or 0
            update_user(uid, total_downloads=current_total + 1)
        if should_send_ad(uid):
            await callback.message.answer(send_ad_text())
            reset_action_count(uid)
    except Exception as e:
        logger.error(f"Error in cb_play: {e}")
        await callback.message.answer(f"❌ Ошибка при загрузке аудио: {e}")

@dp.callback_query(lambda c: c.data.startswith("fav_"))
async def cb_favorite(callback: types.CallbackQuery):
    if is_bot_disabled():
        await callback.message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    video_id = callback.data[len("fav_"):]
    user_id = callback.from_user.id
    increment_action_count(user_id)
    ydl_opts = {'quiet': True, 'nocheckcertificate': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        title = info.get('title', 'Unknown')
        duration = info.get('duration')
        duration_str = format_duration(duration)
        artist = info.get('uploader', 'Unknown Artist')
    cursor.execute(
        """
        INSERT INTO favorites (user_id, video_id, title, artist, duration, created_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, video_id, title, artist, duration_str, datetime.utcnow().isoformat(), 'youtube')
    )
    conn.commit()
    await callback.message.answer("✅ Добавлено в избранное!")
    if should_send_ad(user_id):
        await callback.message.answer(send_ad_text())
        reset_action_count(user_id)

@dp.message(F.text == "🕘 История")
async def cmd_history(message: types.Message):
    if is_bot_disabled():
        await message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    user_id = message.from_user.id
    increment_action_count(user_id)
    cursor.execute("SELECT video_id, title, duration FROM history WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
                   (user_id,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("История пуста.")
        return
    text = "История прослушанных треков:\n"
    for vid, title, duration in rows:
        text += f"{title} [{duration}]\n"
    await message.answer(text)
    if should_send_ad(user_id):
        await message.answer(send_ad_text())
        reset_action_count(user_id)

@dp.message(F.text == "⭐ Избранное")
async def cmd_favorites(message: types.Message):
    if is_bot_disabled():
        await message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    user_id = message.from_user.id
    increment_action_count(user_id)
    cursor.execute("SELECT video_id, title, duration FROM favorites WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
                   (user_id,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("Избранных треков нет.")
        return
    text = "Избранное:\n"
    for vid, title, duration in rows:
        text += f"{title} [{duration}]\n"
    await message.answer(text)
    if should_send_ad(user_id):
        await message.answer(send_ad_text())
        reset_action_count(user_id)

@dp.message(F.text == "🔍 Поиск музыки")
async def cmd_search(message: types.Message, state: FSMContext):
    if is_bot_disabled():
        await message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    await message.answer("🔍 Введите название или исполнителя для поиска на YouTube:")
    await state.set_state(SearchStates.searching)

@dp.message(F.text == "🆕 Новинки")
async def cmd_new_releases(message: types.Message, state: FSMContext):
    if is_bot_disabled():
        await message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    uid = message.from_user.id
    get_user(uid)
    increment_action_count(uid)
    results = await get_new_releases()
    if not results:
        await message.answer("❌ Не удалось найти новинки.")
        return
    await state.update_data(search_results=results)
    await render_search_results(message, results)
    if should_send_ad(uid):
        await message.answer(send_ad_text())
        reset_action_count(uid)

@dp.message(F.text == "🏆 Топ песен")
async def cmd_top_songs(message: types.Message, state: FSMContext):
    if is_bot_disabled():
        await message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    uid = message.from_user.id
    get_user(uid)
    increment_action_count(uid)
    results = await get_top_songs()
    if not results:
        await message.answer("❌ Не удалось найти популярные треки.")
        return
    await state.update_data(search_results=results)
    await render_search_results(message, results)
    if should_send_ad(uid):
        await message.answer(send_ad_text())
        reset_action_count(uid)

@dp.message(F.text == "🌊 Моя волна")
async def cmd_my_wave(message: types.Message, state: FSMContext):
    if is_bot_disabled():
        await message.answer("⚠️ Бот временно отключен. Попробуйте позже.")
        return
    uid = message.from_user.id
    get_user(uid)
    increment_action_count(uid)
    results = await get_my_wave(uid)
    if not results:
        await message.answer("❌ Не удалось сформировать рекомендации.")
        return
    await state.update_data(search_results=results)
    await render_search_results(message, results)
    if should_send_ad(uid):
        await message.answer(send_ad_text())
        reset_action_count(uid)

@dp.message(F.text == "🛠 Админ-панель")
async def cmd_admin_panel(message: types.Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("❌ Доступ запрещён: вы не администратор")
        return
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📢 Отправить рекламу", callback_data="admin_send_ad"))
    kb.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
        InlineKeyboardButton(text="💎 Выдать премиум", callback_data="admin_grant_premium")
    )
    kb.row(
        InlineKeyboardButton(text="🔴 Отключить бота", callback_data="admin_disable_bot"),
        InlineKeyboardButton(text="🟢 Включить бота", callback_data="admin_enable_bot")
    )
    kb.row(InlineKeyboardButton(text="📑 Проверить платежи", callback_data="admin_review_payments"))
    await message.answer("🛠 Админ-панель:", reply_markup=kb.as_markup())

async def get_new_releases():
    russian_artists = ['Мона', 'Наваи', 'Артик', 'Асти', 'Саби', 'Елка', 'Баста', 'Ёлка', 'Дима Билан',
                       'Полина Гагарина', 'Сергей Лазарев', 'Нюша', 'Зиверт', 'Макс Барских', 'ЛСП', 'Клава Кока',
                       'Егор Крид', 'Ольга Бузова', 'Тима Белорусских', 'Мот']
    query = " ".join([f"{artist} новые песни" for artist in russian_artists[:5]]) + " официальный аудио музыка"
    results = await youtube_search(query, max_results=20, max_duration=600, exclude_playlists=True)
    return results

async def get_top_songs():
    russian_artists = ['Мона', 'Наваи', 'Артик', 'Асти', 'Саби', 'Елка', 'Баста', 'Ёлка', 'Дима Билан',
                       'Полина Гагарина', 'Сергей Лазарев', 'Нюша', 'Зиверт', 'Макс Барских', 'ЛСП', 'Клава Кока',
                       'Егор Крид', 'Ольга Бузова', 'Тима Белорусских', 'Мот']
    query = " ".join([f"{artist} популярные песни" for artist in russian_artists[:5]]) + " официальный аудио музыка"
    results = await youtube_search(query, max_results=20, max_duration=600, exclude_playlists=True)
    return results

async def get_my_wave(user_id: int):
    cursor.execute(
        "SELECT artist FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
        (user_id,)
    )
    artists = [row[0] for row in cursor.fetchall()]
    russian_artists = ['Мона', 'Наваи', 'Артик', 'Асти', 'Саби', 'Елка', 'Баста', 'Ёлка', 'Дима Билан',
                       'Полина Гагарина', 'Сергей Лазарев', 'Нюша', 'Зиверт', 'Макс Барских', 'ЛСП', 'Клава Кока',
                       'Егор Крид', 'Ольга Бузова', 'Тима Белорусских', 'Мот']
    if not artists:
        return await get_top_songs()
    artists = [a for a in artists if a in russian_artists] or russian_artists[:5]
    queries = [f"{artist} песня" for artist in artists[:5]]
    query = " ".join(queries) + " русские хиты музыка"
    results = await youtube_search(query, max_results=20, max_duration=600, exclude_playlists=True)
    return results if results else await get_top_songs()

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))
