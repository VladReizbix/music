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

# Загрузка переменных окружения
load_dotenv()

# Получение ключа шифрования
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

# Дешифрование токена
def decrypt_token(encrypted_token, key):
    try:
        cipher = Fernet(key)
        return cipher.decrypt(encrypted_token).decode()
    except Exception as e:
        logger.error(f"Ошибка при дешифровании токена: {e}")
        raise

# Загрузка или создание зашифрованного токена
ENCRYPTION_KEY = get_encryption_key()
cipher = Fernet(ENCRYPTION_KEY)

# Проверка наличия зашифрованного токена
try:
    with open('encrypted_token.bin', 'rb') as token_file:
        ENCRYPTED_TOKEN = token_file.read()
    logger.info("Зашифрованный токен загружен из encrypted_token.bin")
except FileNotFoundError:
    logger.warning("Файл encrypted_token.bin не найден. Создаём новый зашифрованный токен.")
    # Исходный токен (замените на ваш токен и удалите после использования)
    with open('encrypted_token.bin', 'wb') as token_file:
        token_file.write(ENCRYPTED_TOKEN)
    logger.info("Зашифрованный токен сохранён в encrypted_token.bin")

# Дешифрование токена
API_TOKEN = decrypt_token(ENCRYPTED_TOKEN, ENCRYPTION_KEY)

# Константы
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
