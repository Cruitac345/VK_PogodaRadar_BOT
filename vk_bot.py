import os
import random
import time
import csv
import re
import concurrent.futures
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from vkbottle import Bot
from vkbottle.bot import Message
from vkbottle.bot import MessageEvent
from vkbottle import Bot, GroupEventType
from vkbottle import PhotoMessageUploader, DocMessagesUploader
from vkbottle import Keyboard, KeyboardButtonColor, Text, OpenLink, Callback
from vkbottle import TemplateElement
from vkbottle import EMPTY_KEYBOARD
from vkbottle.dispatch.rules.base import GeoRule
from io import BytesIO
import aiohttp
from aiohttp import ClientTimeout
import logging
import typing
import json
import asyncio

logging.getLogger("vkbottle").setLevel(logging.INFO)

# Load environment variables
load_dotenv()

user_game_states = {}  # Для хранения состояний игр пользователей

# Get VK token and check its presence
VK_BOT_TOKEN = os.getenv('VK_BOT_TOKEN')
if not VK_BOT_TOKEN:
    raise ValueError("Error: VK_BOT_TOKEN not set in environment variables (.env file)")

# Check ADMIN_ID
ADMIN_ID = os.getenv('ADMIN_ID')
if not ADMIN_ID:
    raise ValueError("Error: ADMIN_ID not set in environment variables (.env file)")
try:
    ADMIN_ID = int(ADMIN_ID)
except ValueError:
    raise ValueError("Error: ADMIN_ID must be an integer")

# Create bot only if token is valid
bot = Bot(token=VK_BOT_TOKEN)

# Weather API configuration
weather_url = 'http://api.weatherapi.com/v1'
api_key = os.getenv('WEATHER_API_KEY')
lang = 'ru'

@bot.on.raw_event(GroupEventType.MESSAGE_NEW)  # Обрабатываем только новые сообщения
async def message_handler(event: dict):
    # Ваш основной код обработки сообщений
    print("Новое сообщение:", event)

# Асинхронная замена requests.get()
async def fetch_json(url, params=None):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                return await response.json()
        except Exception as e:
            print(f"Request error: {e}")
            return None

# Global variables for game state
user_guess_temp_state = {}
current_handlers = {}  # Для хранения текущих обработчиков

# Добавим функцию для безопасного удаления обработчиков
def clear_user_handlers(user_id):
    if user_id in current_handlers:
        del current_handlers[user_id]

# Добавляем ТОЛЬКО для временных обработчиков:
async def handle_message(message: Message):
    user_id = message.from_id
    if user_id in current_handlers:
        try:
            handler = current_handlers[user_id]
            await handler(message)
            if getattr(handler, "once", False):
                clear_user_handlers(user_id)
        except Exception as e:
            print(f"Ошибка: {e}")
            clear_user_handlers(user_id)
            await message.answer("Ошибка обработки")

# Регистрируем его ТОЛЬКО для сообщений с payload:
@bot.on.message(payload_map={"cmd": str})
async def payload_handler(message: Message):
    await handle_message(message)

# Обработчик временных состояний (ввод города и т.д.)
async def handle_temporary_state(event: typing.Union[Message, MessageEvent]):
    try:
        # Получаем user_id в зависимости от типа события
        user_id = event.from_id if isinstance(event, Message) else event.user_id
        peer_id = event.peer_id if isinstance(event, Message) else event.peer_id
        
        if user_id in current_handlers:
            handler = current_handlers[user_id]
            await handler(event)
            if getattr(handler, "once", False):
                clear_user_handlers(user_id)
            return True
        return False
    except Exception as e:
        print(f"[ERROR] Ошибка в handle_temporary_state: {e}")
        clear_user_handlers(user_id)
        await bot.api.messages.send(peer_id=peer_id, message="⚠️ Произошла ошибка.", random_id=0)
        return True
    
# File names
CITIES_FILE = 'cities.csv'
USER_STATS_FILE = 'user_statistics.csv'

# Flood control settings
FLOOD_LIMIT = 10
FLOOD_INTERVAL = 60
BLOCK_TIME = 60

# Helper functions
def convert_to_mps(kph):
    return kph * 1000 / 3600

def get_wind_direction(deg):
    directions = {
        "N": "Северный",
        "NNE": "Северо-северо-восточный",
        "NE": "Северо-восточный",
        "ENE": "Восточно-северо-восточный",
        "E": "Восточный",
        "ESE": "Восточно-юго-восточный",
        "SE": "Юго-восточный",
        "SSE": "Юго-юго-восточный",
        "S": "Южный",
        "SSW": "Юго-юго-западный",
        "SW": "Юго-западный",
        "WSW": "Западно-юго-западный",
        "W": "Западный",
        "WNW": "Западно-северо-западный",
        "NW": "Северо-западный",
        "NNW": "Северо-северо-западный"
    }
    return directions.get(deg, 'Неизвестное направление')


user_requests = {}  # { "peer_user": { ... } }

def is_flooding(user_id, peer_id=None):
    current_time = time.time()
    identifier = f"{peer_id}_{user_id}" if peer_id else str(user_id)

    if identifier not in user_requests:
        user_requests[identifier] = {
            'last_request_time': current_time,
            'request_count': 1,
            'is_blocked': False,
            'block_until': 0
        }
        return False

    user_data = user_requests[identifier]

    # Проверка на блокировку
    if user_data['is_blocked']:
        if current_time >= user_data['block_until']:
            # Разблокируем
            user_data.update({
                'request_count': 1,
                'last_request_time': current_time,
                'is_blocked': False,
                'block_until': 0
            })
            return False
        return True

    # Сброс счётчика если прошло больше FLOOD_INTERVAL
    if current_time - user_data['last_request_time'] > FLOOD_INTERVAL:
        user_data['request_count'] = 1
        user_data['last_request_time'] = current_time
        return False

    # Увеличиваем счётчик
    user_data['request_count'] += 1
    user_data['last_request_time'] = current_time

    # Проверяем лимит
    if user_data['request_count'] > FLOOD_LIMIT:
        user_data['is_blocked'] = True
        user_data['block_until'] = current_time + BLOCK_TIME
        return True

    return False

# Data storage functions
def save_city(user_id, city_name):
    user_id = str(user_id)
    city_name = city_name.strip()
    if not os.path.exists(CITIES_FILE):
        with open(CITIES_FILE, mode='w', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(['user_id', 'city'])
    data = {}
    with open(CITIES_FILE, mode='r', encoding='utf-8') as file:
        reader = csv.reader(file)
        for row in reader:
            if len(row) == 2:
                data[row[0]] = row[1]
    data[user_id] = city_name
    with open(CITIES_FILE, mode='w', encoding='utf-8') as file:
        writer = csv.writer(file)
        writer.writerow(['user_id', 'city'])
        for uid, city in data.items():
            writer.writerow([uid, city])

def load_city(user_id):
    user_id = str(user_id)
    if not os.path.exists(CITIES_FILE):
        return None
    with open(CITIES_FILE, mode='r', encoding='utf-8') as file:
        reader = csv.reader(file)
        for row in reader:
            if len(row) == 2 and row[0] == user_id:
                return row[1]
    return None

def log_user_activity(user_id, username, action):
    try:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_id = str(user_id)
        action = action.strip()
        
        # Проверяем, не логировали ли мы уже это действие
        if os.path.exists(USER_STATS_FILE):
            with open(USER_STATS_FILE, mode='r', encoding='utf-8') as file:
                reader = csv.reader(file)
                next(reader, None)  # Пропускаем заголовок
                for row in reader:
                    if len(row) >= 3 and row[0] == user_id and row[2] == action:
                        return  # Действие уже залогировано
        
        # Записываем новую запись
        with open(USER_STATS_FILE, mode='a', encoding='utf-8', newline='') as file:
            writer = csv.writer(file)
            if file.tell() == 0:  # Если файл пустой
                writer.writerow(['User ID', 'Username', 'Action', 'Timestamp'])
            writer.writerow([user_id, username, action, current_time])
    except Exception as e:
        print(f"Ошибка при логировании: {e}")

# Load city data for meteograms
def load_city_data(file_path):
    city_data = []
    with open(file_path, mode='r', encoding='utf-8', newline='') as csvfile:
        reader = csv.reader(csvfile)
        for row in reader:
            city_data.append({'eng_name': row[0].strip(), 'rus_name': row[1].strip(), 'url': row[2].strip()})
    return city_data

city_data = load_city_data('city_data.csv')

# Main menu keyboard (only for private messages)
async def get_main_keyboard(user_id=None):
    if user_id and user_id > 0:  # Only for private messages (user_id > 0)
        keyboard = Keyboard(one_time=False, inline=False)
        keyboard.add(Text("🚨Помощь"), color=KeyboardButtonColor.PRIMARY)
        keyboard.add(Text("🗺️Радар"), color=KeyboardButtonColor.PRIMARY)
        keyboard.row()
        keyboard.add(Text("⛅Погода сейчас"), color=KeyboardButtonColor.POSITIVE)
        keyboard.add(Text("📆Погода на 3 дня"), color=KeyboardButtonColor.POSITIVE)
        keyboard.row()
        keyboard.add(Text("✈️Погода в аэропортах"), color=KeyboardButtonColor.SECONDARY)
        keyboard.add(Text("🌫️Качество воздуха"), color=KeyboardButtonColor.SECONDARY)
        keyboard.row()
        keyboard.add(Text("🎁Поддержать"), color=KeyboardButtonColor.NEGATIVE)
        keyboard.add(Text("📢Поделиться ботом"), color=KeyboardButtonColor.NEGATIVE)
        keyboard.row()
        keyboard.add(Text("📊Метеограммы ГМЦ"), color=KeyboardButtonColor.PRIMARY)
        keyboard.row()
        keyboard.add(Text("📍Определить локацию"), color=KeyboardButtonColor.SECONDARY)
        keyboard.row()
        keyboard.add(Text("✏️Изменить город"), color=KeyboardButtonColor.PRIMARY)
        return keyboard.get_json()  # Возвращаем JSON представление клавиатуры
    else:
        # Для группового чата возвращаем пустую клавиатуру
        return None  # Для группового чата возвращаем None

@bot.on.message()
async def message_handler(message: Message):
    # Сначала проверяем временные обработчики
    if await handle_temporary_state(message):
        return
    
    # Проверка на флуд
    if is_flooding(message.from_id, message.peer_id):
        # Получаем время до окончания блокировки
        identifier = f"{message.peer_id}_{message.from_id}"
        user_data = user_requests[identifier]
        remaining_time = int(user_data['block_until'] - time.time())
        
        if remaining_time > 0:
            await message.answer(f"⚠️ Вы заблокированы на {remaining_time} секунд из-за частых запросов.")
        return
    
    # Обработка основных команд
    text = message.text.lower()
    
    commands = {
        ("привет", "начать", "старт", "/start"): start_handler,
        ("помощь", "help", "🚨помощь", "/help"): help_handler,
        ("поддержать", "donate", "🎁поддержать", "/donate"): donate_handler,
        ("поделиться ботом", "share", "📢поделиться ботом", "/share"): share_handler,
        ("изменить город", "setcity", "✏️изменить город", "/setcity"): set_city_handler,
        ("погода сейчас", "nowweather", "⛅погода сейчас", "/nowweather"): now_weather_handler,
        ("погода на 3 дня", "forecastweather", "📆погода на 3 дня", "/forecastweather"): forecast_weather_handler,
        ("качество воздуха", "aqi", "🌫️качество воздуха", "/aqi"): aqi_handler,
        ("радар", "radarmap", "🗺️радар", "/radarmap"): radar_map_handler,
        ("погода в аэропортах", "weatherairports", "✈️погода в аэропортах", "/weatherairports"): airport_weather_handler,
        ("метеограммы гмц", "meteograms", "📊метеограммы гмц", "/meteograms"): meteograms_handler,
        ("определить локацию", "location", "📍определить локацию"): location_handler,
        ("угадать температуру", "guess_temp", "🎮угадать температуру", "/guess_temp"): guess_temp_handler,
        ("метеостанции рф", "stations", "🚩метеостанции рф", "/stations"): stations_handler,
        ("карты meteoweb", "get_meteoweb", "🌍карты meteoweb", "/get_meteoweb"): meteoweb_handler,
        ("экстренная информация", "extrainfo", "❗экстренная информация", "/extrainfo"): extrainfo_handler,
        ("поддержка", "support", "/support"): support_handler,
        ("/precipitationmap",): precipitation_map_handler,
        ("/anomaltempmap",): anomaly_temp_map_handler,
        ("/tempwatermap",): temp_water_map_handler,
        ("/verticaltemplayer",): vertical_temp_handler,
        ("/firehazard_map",): fire_hazard_map_handler,
        ("/alerts",): alerts_handler,
        ("/weatherwebsites",): weather_websites_handler,
    }

    # Проверяем админские команды
    if text in ["статистика", "stats", "/stats"] and message.from_id == ADMIN_ID:
        await stats_handler(message)
        return
    
    # Ищем подходящую команду
    for cmd_tuple, handler in commands.items():
        if text in cmd_tuple:
            await handler(message)
            return
    
# Start command
@bot.on.message(text="/start")
@bot.on.message(payload={"cmd": "start"})
async def start_handler(message: Message):
    log_user_activity(message.from_id, message.from_id, '/start')
    keyboard = await get_main_keyboard(message.peer_id)
    await message.answer(
        "Привет! Я - бот погоды PogodaRadar. Спроси меня о погоде в своем городе или любом другом месте, которое тебя интересует!😊🌦️",
        keyboard=keyboard
    )

# Help command
@bot.on.message(text=["🚨Помощь", "/help"])
async def help_handler(message: Message):
    help_text = (
        "(Техпомощь)\n\n"
        "1) ⚙️Команда /start - Начало работы с ботом\n"
        "2) 🚨Команда /help - Справка о работе с ботом\n"
        "3) 🛠️Команда /support - Связаться с техподдержкой бота\n"
        "(Погода)\n\n"
        "4) ✏️Команда /setcity - Изменить город\n"
        "5) ⛅Команда /nowweather - Текущая погода в городе\n"
        "6) 📆Команда /forecastweather - Прогноз погоды на 3 дня в городе\n"
        "7) ✈️Команда /weatherairports - Погода в аэропортах мира\n"
        "8) 🗺️Команда /radarmap - Радар осадков\n"
        "9) ⚠️Команда /alerts - Предупреждения о непогоде в городах по всему миру\n"
        "10) 🌫️Команда /aqi - Качество воздуха в городе\n"
        "11) ☔Команда /precipitationmap - Карта интенсивности осадков\n"
        "12) 🌡️Команда /anomaltempmap - Карта аномалии среднесуточной температуры за 5 суток\n"
        "13) 🌡️Команда /tempwatermap - Прогноз температуры воды в Черном море\n"
        "14) 📈Команда /verticaltemplayer - Вертикальное распределение температуры в нижнем 1-километровом слое\n"
        "15) 📊Команда /meteograms - Просмотр метеограмм по городам России и Беларуси\n"
        "16) 🌐Команда /weatherwebsites - Полезные сайты для просмотра информации о погоде\n"
        "17) 🔥Команда /firehazard_map - Карта пожароопасности по РФ\n"
        "18) ❗Команда /extrainfo - Экстренная информация об ухудшении погодных условий\n"
        "19) 🚩Команда /stations - Информация о погоде с метеостанций РФ (бета-версия)\n"
        "20) 🌍Команда /get_meteoweb - Прогнозные карты погоды Meteoweb\n"
        "(Доп.настройки)\n\n"
        "21) 📢Команда /share - Поделиться ботом\n"
        "22) 🎁Команда /donate - Поддержать разработчика\n"
        "(Развлечения)\n\n"
        "23) 🎮Команда /guess_temp - Угадай загаданную температуру"
    )
    await message.answer(help_text)

# Support command
@bot.on.message(text=["/support"])
async def support_handler(message: Message):
    await message.answer('🛠️ Для связи с техподдержкой напишите на нашу электронную почту: pogoda.radar@inbox.ru')

# Share command
@bot.on.message(text=["📢Поделиться ботом", "/share"])
async def share_handler(message: Message):
    keyboard = Keyboard(inline=True)
    keyboard.add(OpenLink("https://vk.com/share.php?url=https://vk.com/pogodaradar_bot", "Поделиться ботом"))
    await message.answer(
        "PogodaRadar в VK",
        keyboard=keyboard
    )

# Donate command
@bot.on.message(text=["🎁Поддержать", "/donate"])
async def donate_handler(message: Message):
    donate_text = (
        "Вы можете поддержать PogodaRadar по ссылкам:\n"
        "1) 🎁DonationAlerts:  https://donationalerts.com/r/pogodaradar \n"
        "2) 💶CloudTips: https://pay.cloudtips.ru/p/317d7868 \n"
        "3) 💳YooMoney: https://yoomoney.ru/to/410018154591956 "
    )
    await message.answer(donate_text)

# Set city command
@bot.on.message(text=["✏️Изменить город", "/setcity"])
async def set_city_handler(message: Message):
    user_id = message.from_id
    clear_user_handlers(user_id)  # удаляем старый хандлер, если он есть
    await message.answer('Введите название города:')
    current_handlers[user_id] = process_set_city

async def process_set_city(message: Message):
    user_id = message.from_id
    try:
        if message.text.lower() in ["отмена", "cancel"]:
            await message.answer("❌ Отменено", keyboard=None)
            clear_user_handlers(user_id)
            return
        city = message.text.strip()
        save_city(user_id, city)  # сохраните в CSV или базу данных
        keyboard = await get_main_keyboard(message.peer_id)
        await message.answer(f"✅ Город установлен: {city}", keyboard=keyboard)
    except Exception as e:
        await message.answer("⚠️ Произошла ошибка при установке города.")
        print(f"[ERROR] {e}")
    finally:
        clear_user_handlers(user_id)  # всегда очищаем

# Weather now command (async)
@bot.on.message(text=["⛅Погода сейчас", "/nowweather"])
async def now_weather_handler(message: Message):
    if is_flooding(message.from_id):
        await message.answer("⚠️ Вы заблокированы на 1 минуту из-за частых запросов.")
        return
    city = load_city(message.from_id)
    if city is None:
        await message.answer('Город не установлен. Пожалуйста, сначала используйте команду /setcity, чтобы установить город.')
        return

    parameters = {'key': api_key, 'q': city, 'lang': 'ru'}
    data = await fetch_json(f'{weather_url}/current.json', params=parameters)

    astronomy_parameters = {'key': api_key, 'q': city, 'lang': 'ru'}
    astronomy_data = await fetch_json(f'{weather_url}/astronomy.json', params=astronomy_parameters)

    try:
        location = data['location']['name'] + ', ' + data['location']['country']
        local_time = datetime.strptime(data['location']['localtime'], '%Y-%m-%d %H:%M').strftime('%d %B %Y %H:%M')
        update_current = datetime.strptime(data['current']['last_updated'], '%Y-%m-%d %H:%M').strftime('%d %B %Y %H:%M')

        months = {
            'January': 'Января', 'February': 'Февраля', 'March': 'Марта',
            'April': 'Апреля', 'May': 'Мая', 'June': 'Июня',
            'July': 'Июля', 'August': 'Августа', 'September': 'Сентября',
            'October': 'Октября', 'November': 'Ноября', 'December': 'Декабря'
        }

        local_time = ' '.join([months.get(month, month) for month in local_time.split()])
        update_current = ' '.join([months.get(month, month) for month in update_current.split()])

        condition_code = str(data['current']['condition']['code'])
        condition = data['current']['condition']['text']
        temp_c = data['current']['temp_c']
        feelslike_c = data['current']['feelslike_c']
        wind = data['current']['wind_kph']
        wind_dir = data['current']['wind_dir']
        humidity = data['current']['humidity']
        clouds = data['current']['cloud']
        pressure = int(data['current']['pressure_mb'])
        uv_index = int(data['current']['uv'])
        vis_km = data['current']['vis_km']

        astronomy_data = astronomy_data or {}
        sunrise = astronomy_data.get('astronomy', {}).get('astro', {}).get('sunrise', 'Неизвестно').replace('AM', 'Утра')
        sunset = astronomy_data.get('astronomy', {}).get('astro', {}).get('sunset', 'Неизвестно').replace('PM', 'Вечера')

        weather_icons = {
            '1000': '☀️', '1003': '🌤️', '1006': '☁️', '1009': '☁️',
            '1030': '🌫️', '1063': '🌦️', '1066': '❄️', '1069': '🌨️',
            '1072': '☔', '1087': '🌩️', '1114': '❄️🌬️', '1117': '❄️🌬️',
            '1135': '🌫️', '1147': '🌫️🥶', '1150': '🌧️', '1153': '🌧️',
            '1168': '🌧️', '1171': '🌧️', '1180': '🌧️', '1183': '🌧️',
            '1186': '🌧️', '1189': '🌧️', '1192': '🌧️', '1195': '🌧️',
            '1198': '🌧️❄️', '1201': '🌧️❄️', '1204': '🌨️', '1207': '🌨️',
            '1210': '❄️', '1213': '❄️', '1216': '❄️', '1219': '❄️',
            '1222': '❄️', '1225': '❄️', '1237': '🌨️', '1240': '🌨️',
            '1243': '🌨️', '1246': '🌨️', '1249': '🌨️', '1252': '🌨️',
            '1255': '❄️', '1258': '❄️', '1261': '🌨️', '1264': '🌨️',
            '1273': '⛈️', '1276': '❄️', '1279': '❄️', '1282': '❄️',
        }
        emoji = weather_icons.get(condition_code, '✖️')

        wind_mps = convert_to_mps(wind)
        wind_dir_text = get_wind_direction(wind_dir)

        clothing_recommendations = ''
        if temp_c < -10:
            clothing_recommendations += '❄️ Сильный мороз: Наденьте термобелье, утепленные штаны, пуховик или шубу, шапку-ушанку, шарф, теплые перчатки и зимнюю обувь с мехом.\n'
        elif -10 <= temp_c < 0:
            clothing_recommendations += '❄️ Мороз: Наденьте теплое пальто или пуховик, шапку, шарф, перчатки и утепленную обувь.\n'
        elif 0 <= temp_c < 10:
            clothing_recommendations += '🧥 Прохладно: Наденьте теплую куртку, свитер, джинсы или утепленные брюки, легкую шапку или капюшон.\n'
        elif 10 <= temp_c < 15:
            clothing_recommendations += '🧥 Легкая прохлада: Наденьте ветровку, джинсовку или толстовку, брюки или джинсы.\n'
        elif 15 <= temp_c < 20:
            clothing_recommendations += '👕 Комфортно: Наденьте легкую куртку или кардиган, футболку или рубашку, джинсы или брюки.\n'
        elif 20 <= temp_c < 25:
            clothing_recommendations += '👕 Тепло: Наденьте футболку, шорты или легкие брюки, можно взять с собой легкую кофту на случай ветра.\n'
        else:
            clothing_recommendations += '🔥 Жарко: Наденьте легкую одежду из дышащих тканей, шорты, майку или сарафан. Не забудьте головной убор и солнцезащитные очки.\n'

        if wind >= 40:
            clothing_recommendations += '🌬️ Сильный ветер: Рекомендуем надеть ветровку, плотную куртку и плотные брюки.\n'
        elif wind >= 20:
            clothing_recommendations += '💨 Умеренный ветер: Наденьте легкую блузку, рубашку или футболку и брюки.\n'

        if humidity >= 90:
            clothing_recommendations += '🌧️ Очень высокая влажность: Наденьте водонепроницаемую куртку, непромокаемые штаны и резиновые сапоги. Возьмите зонт.\n'
        elif humidity >= 80:
            clothing_recommendations += '🌧️ Высокая влажность: Наденьте водонепроницаемую куртку и непромокаемую обувь.\n'
        elif humidity >= 60:
            clothing_recommendations += '💦 Повышенная влажность: Наденьте дышащую одежду и обувь, которая не промокает.\n'

        if pressure <= 970:
            clothing_recommendations += '🌪️ Очень низкое давление: Наденьте непромокаемую одежду, возьмите зонт и дополнительный слой одежды на случай резких изменений погоды.\n'
        elif pressure <= 990:
            clothing_recommendations += '🌫️ Низкое давление: Возьмите с собой легкую куртку или свитер, чтобы утеплиться в случае похолодания.\n'
        elif pressure >= 1030:
            clothing_recommendations += '☀️ Высокое давление: Наденьте легкую одежду, так как погода, скорее всего, будет ясной и теплой.\n'

        keyboard = Keyboard(inline=True)
        keyboard.add(OpenLink("https://www.donationalerts.com/r/pogodaradar", "🎁DonationAlerts"))
        keyboard.row()
        keyboard.add(OpenLink("https://pay.cloudtips.ru/p/317d7868", "💶CloudTips"))
        keyboard.row()
        keyboard.add(OpenLink("https://yoomoney.ru/to/410018154591956", "💳YooMoney"))

        weather_message = (
            f'🏙️Погода в городе: {location}\n'
            f'🗓️Время и дата: {local_time}\n'
            f'🔄Данные обновлены: {update_current}\n\n'
            f'{emoji} {condition}\n'
            f'🌡️Температура: {temp_c}°C\n'
            f'🤗По ощущениям: {feelslike_c}°C\n'
            f'💨Скорость ветра: {wind_mps:.1f} м/с\n'
            f'👉🏻Направление ветра: {wind_dir_text}\n'
            f'💧Влажность: {humidity} %\n'
            f'☁️Облачность: {clouds} %\n'
            f'🕗Давление: {pressure} гПа\n'
            f'🕶️Видимость: {vis_km} км\n'
            f'😎UV индекс: {uv_index}\n'
            f'🌅Восход солнца: {sunrise}\n'
            f'🌇Закат солнца: {sunset}\n\n'
            f'Рекомендации по одежде:\n{clothing_recommendations}'
        )
        await message.answer(weather_message, keyboard=keyboard)
    except KeyError:
        await message.answer('Не удалось получить данные о погоде для данного города. Пожалуйста, попробуйте еще раз или укажите другой город.')


# Forecast weather command (async)
@bot.on.message(text=["📆Погода на 3 дня", "/forecastweather"])
async def forecast_weather_handler(message: Message):
    if is_flooding(message.from_id):
        await message.answer("⚠️ Вы заблокированы на 1 минуту из-за частых запросов.")
        return
    city = load_city(message.from_id)
    if city is None:
        await message.answer('Город не установлен. Пожалуйста, сначала используйте команду /setcity, чтобы установить город.')
        return

    parameters = {'key': api_key, 'q': city, 'days': 3, 'lang': 'ru'}
    data = await fetch_json(f'{weather_url}/forecast.json', params=parameters)

    try:
        location = data['location']['name'] + ', ' + data['location']['country']
        forecast_message = f'🏙️Прогноз погоды в городе: {location}\n'

        for day in data['forecast']['forecastday']:
            date = datetime.strptime(day['date'], '%Y-%m-%d').strftime('%d %B %Y')
            months = {
                'January': 'Января', 'February': 'Февраля', 'March': 'Марта',
                'April': 'Апреля', 'May': 'Мая', 'June': 'Июня',
                'July': 'Июля', 'August': 'Августа', 'September': 'Сентября',
                'October': 'Октября', 'November': 'Ноября', 'December': 'Декабря'
            }
            date_parts = date.split()
            formatted_date = ' '.join([months.get(part, part) for part in date_parts])

            condition_code = str(day['day']['condition']['code'])
            conditions = str(day['day']['condition']['text'])
            max_temp = str(day['day']['maxtemp_c'])
            min_temp = str(day['day']['mintemp_c'])
            wind = day['day']['maxwind_kph']
            totalprecip_mm = str(day['day']['totalprecip_mm'])

            weather_icons = {
                '1000': '☀️', '1003': '🌤️', '1006': '☁️', '1009': '☁️',
                '1030': '🌫️', '1063': '🌦️', '1066': '❄️', '1069': '🌨️',
                '1072': '☔', '1087': '🌩️', '1114': '❄️', '1117': '❄️🌬️',
                '1135': '🌫️', '1147': '🌫️🥶', '1150': '🌧️', '1153': '🌦️',
                '1168': '🌦️', '1171': '🌧️', '1180': '🌧️', '1183': '🌧️',
                '1186': '🌧️', '1189': '🌧️', '1192': '🌧️', '1195': '🌧️',
                '1198': '⛈️', '1201': '⛈️', '1204': '⛈️', '1207': '⛈️',
                '1210': '⛈️', '1213': '⛈️', '1216': '霖️', '1219': '霖️',
                '1222': '🌧️', '1225': '🌧️', '1237': '🌨️', '1240': '🌨️',
                '1243': '🌨️', '1246': '🌨️', '1249': '🌨️', '1252': '🌨️',
                '1255': '🌨️', '1258': '🌨️', '1261': '🌨️', '1264': '🌨️',
                '1273': '🌧️', '1276': '❄️', '1279': '❄️', '1282': '❄️',
            }
            emoji = weather_icons.get(condition_code, '✖️')

            wind_mps_forecast = convert_to_mps(wind)

            forecast_message += (
                f'🗓️Дата: {formatted_date}\n\n'
                f'☔Погодные условия: {emoji}{conditions}\n'
                f'🌡️Температура: Днем {max_temp}°C Ночью {min_temp}°C\n'
                f'💨Ветер: {wind_mps_forecast:.1f} м/с\n'
                f'💦Общая сумма осадков за день: {totalprecip_mm} мм\n'
            )

        await message.answer(forecast_message)
    except KeyError:
        await message.answer('Не удалось получить данные о погоде для данного города. Пожалуйста, попробуйте еще раз или укажите другой город.')


# Air quality command (async)
@bot.on.message(text=["🌫️Качество воздуха", "/aqi"])
async def aqi_handler(message: Message):
    if is_flooding(message.from_id):
        await message.answer("⚠️ Вы заблокированы на 1 минуту из-за частых запросов.")
        return
    city = load_city(message.from_id)
    if city is None:
        await message.answer('Город не установлен. Пожалуйста, сначала используйте команду /setcity, чтобы установить город.')
        return

    parameters = {'key': api_key, 'q': city, 'aqi': 'yes', 'lang': 'ru'}
    data = await fetch_json(f'{weather_url}/current.json', params=parameters)

    try:
        location = data['location']['name'] + ', ' + data['location']['country']
        us_epa_index = str(data['current']['air_quality']['us-epa-index'])
        co = str(data['current']['air_quality']['co'])
        no2 = str(data['current']['air_quality']['no2'])
        o3 = str(data['current']['air_quality']['o3'])
        so2 = str(data['current']['air_quality']['so2'])
        pm2_5 = str(data['current']['air_quality']['pm2_5'])
        pm10 = str(data['current']['air_quality']['pm10'])

        aqi_message = (
            f'🏙️Качество воздуха в городе: {location}\n'
            f'🌿Уровень индекса: ( {us_epa_index} )\n'
            f'🏭🔥Среднее значение CO: {co}\n'
            f'🚗🚢Среднее значение NO2: {no2}\n'
            f'🌇Среднее значение O3: {o3}\n'
            f'🏭🌋Среднее значение SO2: {so2}\n'
            f'🏭🚜Среднее значение PM2.5: {pm2_5}\n'
            f'🏭tractorСреднее значение PM10: {pm10}'
        )
        await message.answer(aqi_message)
    except KeyError:
        await message.answer('Ошибка получения данных о качестве воздуха. Пожалуйста, попробуйте еще раз или укажите другой город.')


# Radar map command (async)
@bot.on.message(text=["🗺️Радар", "/radarmap"])
async def radar_map_handler(message: Message):
    url = 'https://meteoinfo.ru/hmc-output/rmap/phenomena.gif'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as response:
                response.raise_for_status()
                file = BytesIO(await response.read())
                file.name = "radar.gif"
                uploader = DocMessagesUploader(bot.api)
                doc = await uploader.upload(
                    file_source=file,
                    file_extension="gif",
                    peer_id=message.peer_id,
                    title="Радар осадков"
                )
                await message.answer("Радар осадков:", attachment=doc)
    except Exception as e:
        await message.answer(f'Не удалось загрузить изображение: {str(e)}')


# Precipitation map command (async)
@bot.on.message(text=["/precipitationmap"])
async def precipitation_map_handler(message: Message):
    url = 'https://meteoinfo.ru/hmc-input/mapsynop/Precip.png'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                response.raise_for_status()
                uploader = PhotoMessageUploader(bot.api)
                photo = await uploader.upload(
                    file_source=BytesIO(await response.read()),
                    peer_id=message.peer_id
                )
                await message.answer("Карта осадков за прошедшие сутки:", attachment=photo)
    except Exception as e:
        await message.answer(f'Не удалось загрузить изображение: {str(e)}')


# Temperature anomaly map command (async)
@bot.on.message(text=["/anomaltempmap"])
async def anomaly_temp_map_handler(message: Message):
    url = 'https://meteoinfo.ru/images/vasiliev/anom2_6/anom2_6.gif'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                response.raise_for_status()
                image_data = await response.read()
                
                # Сначала пробуем загрузить как фото
                try:
                    uploader = PhotoMessageUploader(bot.api)
                    photo = await uploader.upload(
                        file_source=BytesIO(image_data),
                        peer_id=message.peer_id
                    )
                    await message.answer("Карта аномалии температуры:", attachment=photo)
                except Exception as photo_error:
                    # Если не получилось как фото, пробуем как документ
                    try:
                        uploader = DocMessagesUploader(bot.api)
                        doc = await uploader.upload(
                            file_source=BytesIO(image_data),
                            file_extension="png",  # Пробуем как PNG, даже если исходно GIF
                            peer_id=message.peer_id,
                            title="Карта аномалии температуры"
                        )
                        await message.answer("Карта аномалии температуры:", attachment=doc)
                    except Exception as doc_error:
                        await message.answer(f"Не удалось загрузить изображение. Ошибки: фото - {photo_error}, документ - {doc_error}")
                        
    except Exception as e:
        await message.answer(f'Ошибка при получении изображения: {str(e)}')


# Water temperature map command (async)
@bot.on.message(text=["/tempwatermap"])
async def temp_water_map_handler(message: Message):
    url = "https://meteoinfo.ru/res/230/web/esimo/black/sst/black.png"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                response.raise_for_status()
                uploader = PhotoMessageUploader(bot.api)
                photo = await uploader.upload(
                    file_source=BytesIO(await response.read()),
                    peer_id=message.peer_id
                )
                await message.answer("Температура воды в Черном море:", attachment=photo)
    except Exception as e:
        await message.answer(f'Не удалось загрузить изображение: {str(e)}')


# Vertical temperature layer command (async)
@bot.on.message(text=["/verticaltemplayer"])
async def vertical_temp_handler(message: Message):
    url = "https://meteoinfo.ru/hmc-input/profiler/cao/image1.jpg"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                response.raise_for_status()
                uploader = PhotoMessageUploader(bot.api)
                photo = await uploader.upload(
                    file_source=BytesIO(await response.read()),
                    peer_id=message.peer_id
                )
                caption = ("Измерения проведены с помощью оборудования компании НПО АТТЕХ. Координаты профилемера: "
                            "ФГБУ Центральная аэрологическая обсерватория, Московская обл., г. Долгопрудный, ул. Первомайская, 3 "
                            "(55°55´32´´N, 37°31´23´´E)")
                await message.answer(caption, attachment=photo)
    except Exception as e:
        await message.answer(f'Не удалось загрузить изображение: {str(e)}')


# Fire hazard map command (async)
@bot.on.message(text=["/firehazard_map"])
async def fire_hazard_map_handler(message: Message):
    url = "https://meteoinfo.ru/images/vasiliev/plazma_ppo3.gif"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                response.raise_for_status()
                image_data = await response.read()
                
                # Сначала пробуем загрузить как фото
                try:
                    uploader = PhotoMessageUploader(bot.api)
                    photo = await uploader.upload(
                        file_source=BytesIO(image_data),
                        peer_id=message.peer_id
                    )
                    await message.answer("Карта пожароопасности по РФ:", attachment=photo)
                except Exception as photo_error:
                    # Если не получилось как фото, пробуем как документ
                    try:
                        uploader = DocMessagesUploader(bot.api)
                        doc = await uploader.upload(
                            file_source=BytesIO(image_data),
                            file_extension="png",  # Пробуем как PNG, даже если исходно GIF
                            peer_id=message.peer_id,
                            title="Карта пожароопасности"
                        )
                        await message.answer("Карта пожароопасности по РФ:", attachment=doc)
                    except Exception as doc_error:
                        await message.answer(f"Не удалось загрузить изображение. Ошибки: фото - {photo_error}, документ - {doc_error}")
                        
    except Exception as e:
        await message.answer(f'Ошибка при получении изображения: {str(e)}')

# Alerts command (async)
@bot.on.message(text=["/alerts"])
async def alerts_handler(message: Message):
    if is_flooding(message.from_id):
        await message.answer("⚠️ Вы заблокированы на 1 минуту из-за частых запросов.")
        return
    city = load_city(message.from_id)
    if city is None:
        await message.answer('Город не установлен. Пожалуйста, сначала используйте команду /setcity, чтобы установить город.')
        return

    parameters = {'key': api_key, 'q': city, 'days': 1, 'alerts': 'yes', 'lang': 'ru'}
    data = await fetch_json(f'{weather_url}/forecast.json', params=parameters)

    try:
        location = data['location']['name'] + ', ' + data['location']['country']
        local_time = datetime.strptime(data['location']['localtime'], '%Y-%m-%d %H:%M').strftime('%d %B %Y %H:%M')
        months = {
            'January': 'Января', 'February': 'Февраля', 'March': 'Марта',
            'April': 'Апреля', 'May': 'Мая', 'June': 'Июня',
            'July': 'Июля', 'August': 'Августа', 'September': 'Сентября',
            'October': 'Октября', 'November': 'Ноября', 'December': 'Декабря'
        }
        local_time = ' '.join([months.get(month, month) for month in local_time.split()])

        alerts_message = (
            f'🏙️Предупреждения в городе: {location}\n'
            f'🗓️Время и дата: {local_time}\n'
        )

        for alert in data.get('alerts', {}).get('alert', []):
            event = alert.get('event', 'Неизвестное событие')
            desc = alert.get('desc', 'Нет описания')
            effective = datetime.strptime(alert.get('effective', 'Unknown Effective Time'), '%Y-%m-%dT%H:%M:%S%z').strftime('%d %B %Y %H:%M (МСК)')
            expires = datetime.strptime(alert.get('expires', 'Unknown Expiry Time'), '%Y-%m-%dT%H:%M:%S%z').strftime('%d %B %Y %H:%M (МСК)')
            effective = ' '.join([months.get(month, month) for month in effective.split()])
            expires = ' '.join([months.get(month, month) for month in expires.split()])

            alerts_message += (
                f'⚠️Предупреждение: {event}\n'
                f'📝Описание: {desc}\n'
                f'🕙Начальное время: {effective}\n'
                f'🕓Конечное время: {expires}\n'
            )

        await message.answer(alerts_message)
    except KeyError as e:
        await message.answer(f'Ошибка данных: {str(e)}')
    except Exception as e:
        await message.answer(f'Произошла ошибка: {str(e)}')

# Weather websites command (no external request needed)
@bot.on.message(text=["/weatherwebsites"])
async def weather_websites_handler(message: Message):
    websites_text = (
        "Полезные сайты для просмотра погоды:\n"
        "1) ⚡Система грозопеленгации для отслеживания молний по всему миру: https://map.blitzortung.org/#5.13/56.37/40.11\n"
        "2) 🛰️Просмотр архивных спутниковых снимков по Европе и России:  https://zelmeteo.ru\n"
        "3) 📊Сайт для просмотра прогноза погоды прогностических моделей по всему миру: https://meteologix.com"
    )
    await message.answer(websites_text)

# Airport weather command
def get_icao_code_by_name(airport_name):
    airports = {
        # Russia
        "шереметьево": "UUEE", "домодедово": "UUDD", "внуково": "UUWW",
        "жуковский": "UUBW", "абакан": "UNAA", "анадырь": "UHMA",
        "анапа": "URKA", "апатиты": "ULMK", "архангельск": "ULAA",
        "астрахань": "URWA", "барнаул": "UNBB", "белгород": "UUOB",
        "березово": "USHB", "благовещенск": "UNEE", "брянск": "UUBP",
        "бугульма": "UWKB", "великий устюг": "ULWU", "великий новгород": "ULNN",
        "владикавказ": "URMO", "владивосток": "UHWW", "волгоград": "URWW",
        "вологда": "ULWW", "воронеж": "UUOO", "воркута": "UUYW",
        "геленджик": "URKG", "горно-алтайск": "UNBG", "грозный": "URMG",
        "екатеринбург": "USSS", "игарка": "UOII", "ижевск": "USHH",
        "иркутск": "UIII", "йошкар-ола": "UWKJ", "казань": "UWKD",
        "калининград": "UMKK", "калуга": "UUBC", "кемерово": "UNEE",
        "киров": "USKK", "кострома": "UUBA", "краснодар": "URKK",
        "красноярск": "UNKL", "курган": "USUU", "курск": "UUOK",
        "кызыл": "UNKY", "липецк": "UUOL", "магнитогорск": "USCM",
        "махачкала": "URML", "минеральные воды": "URMM", "мурманск": "ULMM",
        "надым": "USMN", "нальчик": "URMN", "нижневартовск": "USNN",
        "нижнекамск": "UWKN", "нижний новгород": "UWGG", "новокузнецк": "UNWW",
        "новосибирск": "UNCC", "новый уренгой": "USMU", "омск": "UNOO",
        "оренбург": "UWOO", "орск": "UWOR", "пенза": "UWPP",
        "пермь": "USPP", "петрозаводск": "ULPB", "петропавловск-камчатский": "UHPP",
        "псков": "ULOO", "ростов-на-дону": "URRR", "рязань": "UWDR",
        "самара": "UWWW", "пулково": "ULLI", "саранск": "UWPS",
        "саратов": "UWSS", "сочи": "URSS", "ставрополь": "URMT",
        "сургут": "USRR", "сыктывкар": "UUYY", "тамбов": "UUOT",
        "томск": "UNTT", "тюмень": "USTR", "ульяновск": "UWLL",
        "уфа": "UWUU", "хабаровск": "UHHH", "ханты-мансийск": "USHN",
        "чебоксары": "UWKS", "челябинск": "USCC", "череповец": "ULWC",
        "чита": "UITA", "южно-сахалинск": "UHSS", "якутск": "UEEE",
        "ярославль": "UUDL",
        # Belarus
        "минск": "UMMS", "минск-1": "UMMM", "брест": "UMBB",
        "витебск": "UMII", "гомель": "UMGG", "гродно": "UMMG",
        "могилев": "UMOO",
    }
    return airports.get(airport_name.lower())

@bot.on.message(text=["✈️Погода в аэропортах", "/weatherairports"])
async def airport_weather_handler(message: Message):
    clear_user_handlers(message.from_id)
    await message.answer('Введите код ICAO (например, UUEE) или название аэропорта (например, Шереметьево). Для отмены введите "отмена"')
    
    async def process_airport_input(msg: Message):
        try:
            if msg.text.lower() in ["отмена", "cancel"]:
                await msg.answer("❌ Отменено")
                clear_user_handlers(msg.from_id)
                return
            
            input_text = msg.text.strip()
            
            # Проверяем, является ли ввод ICAO кодом (4 заглавные буквы)
            if len(input_text) == 4 and input_text.upper().isalpha():
                airport_code = input_text.upper()
            else:
                # Иначе ищем по названию
                airport_code = get_icao_code_by_name(input_text)
                if not airport_code:
                    await msg.answer("Не удалось найти аэропорт. Попробуйте ввести ICAO код (4 буквы) или название аэропорта из списка.")
                    return

            url = f'https://metartaf.ru/{airport_code}.json'
            data = await fetch_json(url)

            if data:
                weather_info = (
                    f"🌐 Кодировка аэропорта: {data['icao']}\n"
                    f"✈️ Погодные условия в аэропорту: {data['name']}\n"
                    f"📍 METAR-сводка по аэропорту: `{data['metar']}`\n"
                    f"🌀 TAF-прогноз по аэропорту: `{data['taf']}`"
                )
                keyboard = Keyboard(inline=True)
                keyboard.add(Callback("Как расшифровать данные?", {"cmd": "decode_airport"}))
                await msg.answer(weather_info, keyboard=keyboard)
            else:
                await msg.answer("Ошибка получения данных о погоде. Проверьте правильность кода аэропорта.")
        finally:
            clear_user_handlers(msg.from_id)
    
    current_handlers[message.from_id] = process_airport_input
    process_airport_input.once = True

@bot.on.raw_event(GroupEventType.MESSAGE_EVENT, MessageEvent, payload_contains={"cmd": "decode_airport"})
async def handle_decode_airport(event: MessageEvent):
    # Подтверждаем получение события
    try:
        await bot.api.messages.send_message_event_answer(
            event_id=event.object.event_id,
            user_id=event.object.user_id,
            peer_id=event.object.peer_id
        )
    except Exception as e:
        print(f"[ERROR] Ошибка подтверждения callback: {e}")
    
    how_to_message = (
        "🛠 Как расшифровать METAR и TAF самостоятельно:\n\n"
        "📄 **METAR** — это закодированное сообщение о текущей погоде на аэродроме.\n"
        "Основные элементы METAR:\n"
        "- ICAO-код аэропорта (например, UUEE — Шереметьево)\n"
        "- Время составления прогноза (например, 121200Z — 12-е число, 12:00 UTC)\n"
        "- Погодные условия: облачность, видимость, осадки (например, SCT030 — разбросанные облака на высоте 3000 футов)\n"
        "- Ветер: направление и скорость (например, 18010KT — ветер с юга, 10 узлов)\n"
        "\n"
        "📄 **TAF** — прогноз погоды для аэродрома на определенный период.\n"
        "Ключевые элементы TAF:\n"
        "- Время действия прогноза (например, 1212/1312 — с 12:00 12-го числа до 12:00 13-го числа)\n"
        "- Изменения погоды: TEMPO, BECMG, PROB (например, TEMPO 1418 — временные изменения с 14:00 до 18:00)\n\n"
        "📌 Для детального разбора каждого элемента вы можете воспользоваться ссылкой:\n"
        "https://www.iflightplanner.com/resources/metartaftranslator.aspx "
    )
    
    # Используем bot.api.messages.send вместо event.answer
    await bot.api.messages.send(
        peer_id=event.object.peer_id,
        message=how_to_message,
        random_id=0,
        dont_parse_links=True
    )

# Meteograms command
@bot.on.message(text=["📊Метеограммы ГМЦ", "/meteograms"])
async def meteograms_handler(message: Message):
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("Один город", {"cmd": "meteo_one_city"}))
    keyboard.add(Callback("Несколько городов", {"cmd": "meteo_several_cities"}))
    await message.answer("Выберите режим:", keyboard=keyboard)

@bot.on.raw_event(GroupEventType.MESSAGE_EVENT, MessageEvent, payload_contains={"cmd": "meteo_one_city"})
async def handle_meteo_one_city(event: MessageEvent):
    user_id = event.object.user_id
    peer_id = event.object.peer_id
    
    # Подтверждаем получение события
    try:
        await bot.api.messages.send_message_event_answer(
            event_id=event.object.event_id,
            user_id=user_id,
            peer_id=peer_id
        )
    except Exception as e:
        print(f"[ERROR] Ошибка подтверждения callback: {e}")
    
    try:
        # Отправляем сообщение пользователю
        await bot.api.messages.send(
            peer_id=peer_id,
            message="Введите название города:",
            random_id=0
        )
        
        # Устанавливаем обработчик для следующего сообщения
        async def process_city_input(msg: Message):
            try:
                if msg.text.lower() in ["отмена", "cancel"]:
                    await msg.answer("❌ Отменено")
                    return
                
                city_name = msg.text.strip().upper()
                city_info = next((city for city in city_data if city['rus_name'].upper() == city_name or city['eng_name'].upper() == city_name), None)

                if not city_info:
                    await msg.answer("Город не найден. Попробуйте еще раз.")
                    return

                # Начинаем замер времени
                start_time = time.time()
                
                # Загружаем изображение
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(city_info['url'], timeout=ClientTimeout(total=30)) as response:
                            if response.status == 200:
                                image_data = await response.read()
                                uploader = PhotoMessageUploader(bot.api)
                                photo = await uploader.upload(
                                    file_source=BytesIO(image_data),
                                    peer_id=msg.peer_id
                                )
                                
                                # Вычисляем затраченное время
                                elapsed_time = round(time.time() - start_time, 2)
                                
                                await msg.answer(
                                    f'📊 Прогноз на 5 дней для города: {city_info["rus_name"]}\n'
                                    f'⏱️ Время загрузки: {elapsed_time} сек.',
                                    attachment=photo
                                )
                            else:
                                await msg.answer(f"❌ Не удалось загрузить метеограмму для города {city_info['rus_name']}")
                except Exception as e:
                    await msg.answer(f"❌ Ошибка при загрузке изображения: {str(e)}")
                    
            finally:
                clear_user_handlers(msg.from_id)
        
        current_handlers[user_id] = process_city_input
        process_city_input.once = True
        
    except Exception as e:
        print(f"[ERROR] Error in handle_meteo_one_city: {e}")
        await bot.api.messages.send(
            peer_id=peer_id,
            message="⚠️ Произошла ошибка при обработке запроса",
            random_id=0
        )

@bot.on.raw_event(GroupEventType.MESSAGE_EVENT, MessageEvent, payload_contains={"cmd": "meteo_several_cities"})
async def handle_meteo_several_cities(event: MessageEvent):
    user_id = event.object.user_id
    peer_id = event.object.peer_id
    
    # Подтверждаем получение события
    try:
        await bot.api.messages.send_message_event_answer(
            event_id=event.object.event_id,
            user_id=user_id,
            peer_id=peer_id
        )
    except Exception as e:
        print(f"[ERROR] Ошибка подтверждения callback: {e}")
    
    try:
        # Отправляем сообщение пользователю
        await bot.api.messages.send(
            peer_id=peer_id,
            message="Введите названия городов через запятую (максимум 10):",
            random_id=0
        )
        
        # Устанавливаем обработчик для следующего сообщения
        async def process_cities_input(msg: Message):
            try:
                if msg.text.lower() in ["отмена", "cancel"]:
                    await msg.answer("❌ Отменено")
                    return
                
                cities = [city.strip().upper() for city in msg.text.split(',') if city.strip()][:10]
                found_cities = []
                
                for city_name in cities:
                    city_info = next((city for city in city_data if city['rus_name'].upper() == city_name or city['eng_name'].upper() == city_name), None)
                    if city_info:
                        found_cities.append(city_info)
                
                if not found_cities:
                    await msg.answer("Ни один из указанных городов не найден.")
                    return
                
                # Начинаем общий замер времени
                total_start_time = time.time()
                successful_cities = 0
                
                # Загружаем и отправляем метеограммы
                for city in found_cities:
                    try:
                        city_start_time = time.time()
                        async with aiohttp.ClientSession() as session:
                            async with session.get(city['url'], timeout=ClientTimeout(total=30)) as response:
                                if response.status == 200:
                                    image_data = await response.read()
                                    uploader = PhotoMessageUploader(bot.api)
                                    photo = await uploader.upload(
                                        file_source=BytesIO(image_data),
                                        peer_id=msg.peer_id
                                    )
                                    
                                    city_elapsed_time = round(time.time() - city_start_time, 2)
                                    
                                    await msg.answer(
                                        f'📊 Прогноз на 5 дней для города: {city["rus_name"]}\n'
                                        f'⏱️ Время загрузки: {city_elapsed_time} сек.',
                                        attachment=photo
                                    )
                                    successful_cities += 1
                                else:
                                    await msg.answer(f"❌ Не удалось загрузить метеограмму для города {city['rus_name']}")
                    except Exception as e:
                        await msg.answer(f"❌ Ошибка при загрузке метеограммы для {city['rus_name']}: {str(e)}")
                
                # Общее время выполнения
                total_elapsed_time = round(time.time() - total_start_time, 2)
                
                await msg.answer(
                    f"✅ Готово!\n"
                    f"📊 Успешно загружено: {successful_cities} из {len(found_cities)}\n"
                    f"⏱️ Общее время: {total_elapsed_time} сек."
                )
            finally:
                clear_user_handlers(msg.from_id)
        
        current_handlers[user_id] = process_cities_input
        process_cities_input.once = True
        
    except Exception as e:
        print(f"[ERROR] Error in handle_meteo_several_cities: {e}")
        await bot.api.messages.send(
            peer_id=peer_id,
            message="⚠️ Произошла ошибка при обработке запроса",
            random_id=0
        )

# Meteoweb maps command (async)
# Глобальная переменная type_mapping (должна быть определена до использования)
type_mapping = {
    "prec": ("prec", "🌧️ Осадки"),
    "temp": ("temp", "🌡️ Температура у поверхности 2м"),
    "temp8": ("temp8", "🌡️ Температура на уровне 850 гПа"),
    "cloudst": ("cloudst", "☁️ Низкая-средняя облачность"),
    "cloudsh": ("cloudsh", "☁️ Верхняя облачность"),
    "wind": ("wind", "💨 Ветер"),
    "licape": ("licape", "⚡ Параметры неустойчивости"),
    "snd": ("snd", "❄️ Высота снежного покрова"),
    "tef": ("tef", "🌡️ Эффективная температура")
}

async def get_session():
    return aiohttp.ClientSession(timeout=ClientTimeout(total=10))

# Функция получения URL карты
def get_fmeteo_image_and_info(run_time, forecast_hour, map_type="prec"):
    if map_type not in type_mapping:
        return f"Ошибка: неверный тип карты для fmeteo. Поддерживаются: {', '.join(type_mapping.keys())}", None, None
    type_code, map_type_text = type_mapping[map_type]
    url = f"http://fmeteo.ru/gfs/{run_time}/{type_code}_{forecast_hour}.png"
    return url, "", map_type_text  # URL без проверки — загрузка отдельно

# Обработчик команды /get_meteoweb
@bot.on.message(text=["/get_meteoweb"])
async def meteoweb_handler(message: Message):
    instruction = (
        "🌍 *Команда /get_meteoweb* — ваш помощник для получения прогнозных карт погоды от Meteoweb!\n"
        "📝 *Как использовать:*\n"
        "Введите параметры карты в формате:\n"
        "`время_прогона начальный_час конечный_час тип_карты`\n\n"
        "🔍 *Примеры запросов:*\n"
        "• `00 003 027 prec` — карта осадков с 3 по 27 час прогноза.\n"
        "• `12 006 036 temp` — карта температуры у поверхности с 6 по 36 час.\n"
        "• `00 003 024 temp8` — карта температуры на уровне 850 гПа с 3 по 24 час.\n\n"
        "📊 *Доступные типы карт:*\n"
        "• `prec` — осадки 🌧️\n"
        "• `temp` — температура у поверхности 🌡️\n"
        "• `temp8` — температура на уровне 850 гПа 🗻\n"
        "• `cloudst` — общая облачность ☁️\n"
        "• `cloudsh` — высокая облачность 🌫️\n"
        "• `wind` — ветер 🌬️\n"
        "• `licape` — индекс неустойчивости (LICAPE) ⚡\n"
        "• `snd` — снежный покров ❄️\n"
        "• `tef` — температура эффективная 🌡️\n\n"
        "⚠️ *Важные ограничения:*\n"
        "• За один запрос можно получить не более 10 карт.\n"
        "• Если нужно больше карт, повторите команду."
    )
    await message.answer(instruction)
    user_id = message.from_id
    clear_user_handlers(user_id)

    async def process_meteoweb_request(msg: Message):
        try:
            parts = msg.text.split()
            if len(parts) != 4:
                raise ValueError("Неверное количество параметров. Ожидается: время прогона, начальный час, конечный час, тип карты.")
            run_time = parts[0]
            start_hour = int(parts[1])
            end_hour = int(parts[2])
            map_type = parts[3].lower()

            if run_time not in ["00", "06", "12", "18"]:
                raise ValueError("Неверное время прогона. Допустимые значения: 00, 06, 12, 18.")
            if not (3 <= start_hour <= 384 and start_hour % 3 == 0):
                raise ValueError("Некорректное начальное время прогноза. Время должно быть от 003 до 384 с шагом в 3 часа.")
            if not (3 <= end_hour <= 384 and end_hour % 3 == 0):
                raise ValueError("Некорректное конечное время прогноза. Время должно быть от 003 до 384 с шагом в 3 часа.")
            if start_hour > end_hour:
                raise ValueError("Начальное время не может быть больше конечного.")
            if map_type not in type_mapping:
                raise ValueError(f"Неверный тип карты. Допустимые значения: {', '.join(type_mapping.keys())}.")

            forecast_hours = list(range(start_hour, end_hour + 1, 3))
            max_images_per_request = 10
            if len(forecast_hours) > max_images_per_request:
                await msg.answer(
                    f"⚠️ Запрос превышает лимит: можно получить только {max_images_per_request} карт за один запрос. "
                    f"Попробуйте уменьшить диапазон времени."
                )
                return

            urls = [
                f"http://fmeteo.ru/gfs/{run_time}/{map_type}_{hour:03}.png"
                for hour in forecast_hours
            ]

            attachments = []
            session = await get_session()
            for url in urls:
                async with session.get(url) as response:
                    if response.status == 200:
                        file = BytesIO(await response.read())
                        file.name = os.path.basename(url)
                        uploader = DocMessagesUploader(bot.api)
                        doc = await uploader.upload(
                            file_source=file,
                            file_extension="png",
                            peer_id=msg.peer_id,
                            title="Карта"
                        )
                        attachments.append(doc)

            caption = (
                f"📅 Прогноз погоды с {calculate_forecast_time(run_time, start_hour)} по {calculate_forecast_time(run_time, end_hour)}\n"
                f"Тип карты: {type_mapping[map_type][1]}"
            )

            if attachments:
                await msg.answer(caption, attachment=','.join(attachments))
            else:
                await msg.answer("Не удалось загрузить изображения.")

        except Exception as e:
            await msg.answer(f"Произошла ошибка: {str(e)}")

    current_handlers[user_id] = process_meteoweb_request
    process_meteoweb_request.once = True

def calculate_forecast_time(run_time, forecast_hour):
    run_time_hour = int(run_time)
    forecast_hour = int(forecast_hour)
    current_date = datetime.utcnow()
    forecast_date = current_date.replace(hour=run_time_hour, minute=0, second=0, microsecond=0)
    if current_date.hour < run_time_hour:
        forecast_date -= timedelta(days=1)
    forecast_date += timedelta(hours=forecast_hour)
    return forecast_date.strftime("%Y-%m-%d %H:%M") + " UTC"


# Extra info command (async)
@bot.on.message(text=["/extrainfo"])
async def extrainfo_handler(message: Message):
    url = 'https://meteoinfo.ru/extrainfopage'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                response.raise_for_status()
                text = await response.text()
        soup = BeautifulSoup(text, 'html.parser')
        page_header = soup.find('div', class_='page-header')
        headline = page_header.find('h1').text.strip() if page_header and page_header.find('h1') else "Экстренная информация"

        extrainfo = []
        info_blocks = soup.find_all('div', id='div_1')
        for block in info_blocks:
            rows = block.find_all('tr')
            for row in rows:
                cells = row.find_all('td')
                cell_texts = [cell.get_text(strip=True) for cell in cells]
                if cell_texts:
                    extrainfo.append(" | ".join(cell_texts))

        extrainfo = extrainfo[:7] or ["Нет экстренной информации."]

        additional_info = []
        div_2 = soup.find('div', id='div_2')
        if div_2:
            for row in div_2.find_all('tr'):
                cell = row.find('td')
                if cell and cell.text.strip():
                    additional_info.append(cell.text.strip())

        combined_message = f"⚠️ {headline} ⚠️\n" + "\n".join(extrainfo)
        combined_message += "\n— — —\n" + ("\n".join(additional_info) if additional_info else "Нет дополнительной информации.")
        await message.answer(combined_message)
    except Exception as e:
        await message.answer(f"Ошибка при получении данных: {str(e)}")

# Функция для получения данных о погоде с сайта
regions_dict = {
    "адыгея республика": "republic-adygea",
    "алтай республика": "republic-altai",
    "алтайский край": "territory-altai",
    "амурская область": "amur-area",
    "архангельская область": "arkhangelsk-area",
    "астраханская область": "astrakhan-area",
    "башкортостан республика": "republic-bashkortostan",
    "белгородская область": "belgorod-area",
    "брянская область": "bryansk-area",
    "бурятия республика": "republic-buryatia",
    "владимирская область": "vladimir-area",
    "волгоградская область": "volgograd-area",
    "вологодская область": "vologda-area",
    "воронежская область": "voronezh-area",
    "дагестан республика": "republic-dagestan",
    "донецкая народная республика": "republic-donetsk",
    "еврейская автономная область": "evr-avt-obl",
    "забайкальский край": "territory-zabaykalsky",
    "запорожская область": "zaporizhzhia-area",
    "ивановская область": "ivanovo-area",
    "ингушетия республика": "republic-ingushetia",
    "иркутская область": "irkutsk-area",
    "кабардино-балкария республика": "republic-kabardino-balkaria",
    "калининградская область": "kaliningrad-area",
    "калмыкия республика": "republic-kalmykia",
    "калужская область": "kaluga-area",
    "камчатский край": "territory-kamchatka",
    "карачаево-черкесия": "republic-karachay-cherkessia",
    "карелия республика": "republic-karelia",
    "кемеровская область": "kemerovo-area",
    "кировская область": "kirov-area",
    "коми республика": "republic-komi",
    "костромская область": "kostroma-area",
    "краснодарский край": "krasnodar-territory",
    "красноярский край": "territory-krasnoyarsk",
    "крым республика": "republic-crimea",
    "курганская область": "kurgan-area",
    "курская область": "kursk-area",
    "ленинградская область": "leningrad-region",
    "липецкая область": "lipetsk-area",
    "луганская народная республика": "republic-lugansk",
    "магаданская область": "magadan-area",
    "марий эл республика": "republic-mari-el",
    "мордовия республика": "republic-mordovia",
    "московская область": "moscow-area",
    "мурманская область": "murmansk-area",
    "ненецкий автономный округ": "autonomous-area-nenets",
    "нижегородская область": "nizhny-novgorod-area",
    "новгородская область": "novgorod-area",
    "новосибирская область": "novosibirsk-area",
    "омская область": "omsk-area",
    "оренбургская область": "orenburg-area",
    "орловская область": "oryol-area",
    "пензенская область": "penza-area",
    "пермский край": "territory-perm",
    "приморский край": "territory-primorsky",
    "псковская область": "pskov-area",
    "ростовская область": "rostov-area",
    "рязанская область": "ryazan-area",
    "самарская область": "samara-area",
    "саратовская область": "saratov-area",
    "саха(якутия) республика": "republic-sakha-yakutia",
    "сахалинская область": "sakhalin-area",
    "свердловская область": "sverdlovsk-area",
    "северная осетия-алания республика": "republic-north-ossetia-alania",
    "смоленская область": "smolensk-area",
    "ставропольский край": "territory-stavropol",
    "тамбовская область": "tambov-area",
    "татарстан республика": "republic-tatarstan",
    "тверская область": "tver-area",
    "томская область": "tomsk-area",
    "тульская область": "tula-area",
    "тыва республика": "republic-tyva",
    "тюменская область": "tyumen-area",
    "удмуртия республика": "republic-udmurtia",
    "ульяновская область": "ulyanovsk-area",
    "хабаровский край": "territory-khabarovsk",
    "хакасия республика": "republic-khakassia",
    "ханты-мансийский автономный округ": "autonomous-area-khanty-mansi",
    "херсонская область": "kherson-region",
    "челябинская область": "chelyabinsk-area",
    "чеченская республика": "republic-chechen",
    "чувашская республика": "republic-chuvash",
    "чукотский автономный округ": "autonomous-area-chukotka",
    "ямало-ненецкий ао": "autonomous-area-yamalo-nenets",
    "ярославская область": "yaroslavl-area",
}

# Словарь станций
stations_dict = {
    "клин": "klin",
    "москва": "moscow",
    "калуга": "kaluga-A",
    "тверь": "tver",
    "быково": "bykovo",
    "внуково": "vnukovo",
    "волоколамск": "volokolamsk",
    "дмитров": "dmitrov",
    "домодедово": "domodedovo",
    "егорьевск": "egorevsk",
    "каширa": "kashira",
    "коломна": "kolomna",
    "можайск": "mozhaysk",
    "москва вднх": "moscow",
    "москва балчуг": "moskva-balchug",
    "наро-фоминск": "naro-fominsk",
    "немчиновка": "nemchinovka",
    "ново-иерусалим": "novo-jerusalim",
    "орехово-зуево": "orekhovo-zuevo",
    "павловский посад": "pavlovsky-posad",
    "павловское": "pavlovskoe",
    "сергиев посад": "sergiev-posad",
    "серпухов": "serpukhov",
    "третьяково": "tretyakovo",
    "черусти": "cherusti",
    "шереметьево": "sheremetyevo",
    "железногорск": "zheleznogorsk",
    "курск": "kursk",
    "курчатов": "kurchatov",
    "обоянь": "oboyan",
    "поныри": "ponyri",
    "рыльск": "rylsk",
    "тим": "tim",
    "майкоп": "majkop",
    "горно-алтайск": "gorno-altaysk",
    "барнаул": "barnaul",
    "благовещенск": "blagoveshchensk",
    "архангельск": "arkhangelsk",
    "астрахань": "astrakhan",
    "уфа": "ufa",
    "белгород": "belgorod",
    "брянск": "bryansk",
    "улан-удэ": "ulan-ude",
    "владимир": "vladimir",
    "волгоград": "volgograd",
    "вологда": "vologda",
    "воронеж": "voronezh",
    "махачкала": "makhachkala",
    "донецк": "donetsk",
    "биробиджан": "birobidzhan",
    "чита": "chita",
    "бердянск": "berdyansk",
    "иваново": "ivanovo",
    "назарян": "nazran",
    "иркутск": "irkutsk",
    "нальчик": "nalchik",
    "калининград": "kaliningrad",
    "элиста": "elista",
    "петропавловск": "petropavlovsk",
    "черкесск": "cherkessk",
    "петрозаводск": "petrozavodsk",
    "кемерово": "kemerovo",
    "киров": "kirov",
    "сыктывкар": "syktyvkar",
    "кострома": "kostroma",
    "краснодар": "krasnodar",
    "красноярск": "krasnoyarsk",
    "симферополь": "simferopol",
    "курган": "kurgan",
    "липецк": "lipetsk",
    "луганск": "luhansk",
    "магадан": "magadan",
    "йошкар-ола": "joskar-ola",
    "саранск": "saransk",
    "мурманск": "murmansk",
    "нарьян-мар": "naryan-mar",
    "нижний новгород": "nizhny-novgorod",
    "новгород": "novgorod",
    "новосибирск": "novosibirsk",
    "омск": "omsk",
    "оренбург": "orenburg",
    "орёл": "orel",
    "пенза": "penza",
    "пермь": "perm",
    "владивосток": "vladivostok",
    "псков": "pskov",
    "ростов-на-дону": "rostov-na-donu",
    "рязань": "ryazan",
    "самара": "samara",
    "саратов": "saratov",
    "якутск": "yakutsk",
    "южно-сахалинск": "yuzhno-sakhalinsk",
    "екатеринбург": "yekaterinburg",
    "владикавказ": "vladikavkaz",
    "смоленск": "smolensk",
    "ставрополь": "stavropol",
    "тамбов": "tambov",
    "казань": "kazan",
    "абакан": "abakan",
    "тюмень": "tyumen",
    "ижевск": "izhevsk",
    "ульяновск": "ulyanovsk",
    "хабаровск": "khabarovsk",
    "грозный": "grozny",
    "чебоксары": "cheboksary",
    "анадырь": "anadyr",
    "салехард": "salehard",
    "вязьма": "vyazma",
    "гагарин": "gagarin",
    "рославль": "roslavl",
    "смоленск": "smolensk",
    "жердевка": "zerdevka",
    "кирсанов": "kirsanov",
    "мичуринск": "michurinsk",
    "моршанск": "morshansk",
    "обловка": "oblovka",
    "совхоз им.ленина": "sovkhoz_im_len",
    "тамбов амсг": "tambov",
    "анапа": "anapa",
    "армавир": "armavir",
    "белая глина": "belaya_glina",
    "геленджик": "gelendzhik",
    "горячий ключ": "goryachiy_klyuch",
    "джубга": "dzhubga",
    "должанская": "dolzhanskaya",
    "ейск": "eysk",
    "каневская": "kanevskaya",
    "красная поляна": "krasnaya_polyana",
    "краснодар": "krasnodar",
    "кропоткин": "kropotkin",
    "крымск": "krymsk",
    "кубанская": "kubanskaya",
    "кущевская": "kushchevskaya",
    "новороссийск": "novorossiysk",
    "приморско-ахтарск": "primorsko_akhtarsk",
    "славянск-на-кубани": "slavyansk_na_kubani",
    "сочи": "sochi_adler",
    "тамань": "tamany",
    "тихорецк": "tikhoretsk",
    "туапсе": "tuapse",
    "усть-лабинск": "ust_labinsk",
    "белогорка": "belogorka",
    "винницы": "vinnitsy",
    "вознесенье": "voznesenye",
    "волосово": "volosovo",
    "выборг": "vyborg",
    "ефимовская": "efimovskaya",
    "кингисепп": "kingisepp",
    "кириши": "kirishi",
    "лодейное поле": "lodeynoye_pole",
    "луга": "luga",
    "николаевская": "nikolaevskaya",
    "новая ладога": "novaya_ladoga",
    "озерки": "ozerki",
    "петрокрепость": "petrokrepost",
    "приозерск": "priozersk",
    "санкт-петербург": "sankt_peterburg",
    "сосново": "sosnovo",
    "тихвин": "tikhvin",
    "переславль-залесский": "pereslavl_zalesskiy",
    "пошехонье": "poshekhonye",
    "ростов": "rostov",
    "рыбинск": "rybinsk",
    "ярославль": "yaroslavl",
    "волово": "volovo",
    "ефремов": "efremov",
    "новомосковск": "novomoskovsk",
    "тула": "tula",
    "анна": "anna",
    "богучар": "boguchar",
    "борисоглебск": "borisoglebsk",
    "воронеж": "voronezh_1",
    "калач": "kalach",
    "лиски": "liski",
    "павловск": "pavlovsk",
    "арзамас": "arzamas",
    "ветлуга": "vetluga",
    "воскресенское": "voskresenskoe",
    "выкса": "vyksa",
    "городец волжская гмо": "gorodets_volzhskaya_gmo",
    "красные баки": "krasnye_baki",
    "лукоянов": "lukoyanov",
    "лысково": "lyskovo",
    "нижний новгород-1": "nizhny_novgorod",
    "павлово": "pavlovo",
    "сергач": "sergach",
    "шахунья": "shakhunya",
    "алапаевск": "alapaevsk",
    "артемовский": "artemovsky",
    "бисерть": "biserte",
    "верхнее дуброво": "verhnee_dubrovo",
    "верхотурье": "verhoturye",
    "висим": "visim",
    "гари": "gari",
    "екатеринбург": "ekaterinburg",
    "ивдель": "ivdel",
    "ирбит-фомино": "irbit_fomino",
    "каменск-уральский": "kamensk_uralsky",
    "камышлов": "kamyshlov",
    "кольцово": "kolcovo",
    "красноуфимск": "krasnoufimsk",
    "кушва": "kushva",
    "кытлым": "kytlym",
    "михайловск": "mihaylovsk",
    "невьянск": "nev'yansk",
    "нижний тагил": "nizhny_tagil",
    "понил": "ponil",
    "ревда": "revda",
    "североуральск": "severouralsk",
    "серов": "serov",
    "сысерть": "sysert",
    "таборы": "tabory",
    "тавда": "tavda",
    "тугулым": "tugulym",
    "туринск": "turinsk",
    "шамары": "shamary",
    "волгоград": "volgograd",
    "волжский": "volzhsky",
    "даниловка": "danilovka",
    "елань": "elan",
    "иловля": "ilovlya",
    "камышин": "kamyshin",
    "михайловка": "mihailovka",
    "нижний чир": "nizhny_chir",
    "паласовка": "pallasovka",
    "серафимович": "serafimovich",
    "урюпинск": "uryupinsk",
    "фролово": "frolovo",
    "эльтон": "elton",
    "большие кайбицы": "bolshie_kaybitsy",
    "бугульма": "bugulma",
    "елабуга": "elabuga",
    "казань": "kazan",
    "лаишево": "laishevo",
    "муслюмово": "muslyumovo_1",
    "набережные челны": "naberezhnye_chelny",
    "тетюши": "tetyushi",
    "чистополь": "chistopol",
    "чистополь": "chistopol_b",
    "чулпаново": "chulpanovo"
}

# Stations command (async)
@bot.on.message(text=["🚩Метеостанции РФ", "/stations"])
async def stations_handler(message: Message):
    await message.answer("Введите регион (например, Московская область):")
    user_id = message.from_id
    clear_user_handlers(user_id)
    current_handlers[user_id] = process_region


async def process_region(msg: Message):
    if msg.text.lower() in ["отмена", "cancel"]:
        await msg.answer("❌ Отменено", keyboard=EMPTY_KEYBOARD)
        clear_user_handlers(msg.from_id)
        return
    region_name = msg.text.lower().strip()
    if region_name not in regions_dict:
        await msg.answer("регион не найден. Проверьте правильность написания.")
        return
    region_code = regions_dict[region_name]
    await msg.answer("Введите название станции (например, Клин):")
    current_handlers[msg.from_id] = lambda m: process_station(m, region_code)


async def process_station(msg: Message, region_code: str):
    if msg.text.lower() in ["отмена", "cancel"]:
        await msg.answer("❌ Отменено", keyboard=EMPTY_KEYBOARD)
        clear_user_handlers(msg.from_id)
        return
    station_name = msg.text.lower().strip()
    if station_name not in stations_dict:
        await msg.answer("Станция не найдена. Проверьте правильность написания.")
        return
    station_code = stations_dict[station_name]
    url = f"https://meteoinfo.ru/pogoda/russia/{region_code}/{station_code}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                response.raise_for_status()
                html = await response.text()
        soup = BeautifulSoup(html, "html.parser")
        update_time = soup.find("td", {"colspan": "2", "align": "right"})
        update_time = update_time.text.strip() if update_time else "Нет данных о времени обновления"

        table = soup.find("table", {"border": "0", "style": "width:100%"})
        if not table:
            await msg.answer("Не удалось найти данные о погоде для указанной станции.")
            return

        weather_data = {}
        for row in table.find_all("tr"):
            columns = row.find_all("td")
            if len(columns) == 2:
                parameter = columns[0].text.strip()
                value = columns[1].text.strip()
                weather_data[parameter] = value

        message_text = (
            f"📍 Погода для станции: {station_name.capitalize()}\n"
            f"🕒 Обновлено: {update_time}\n"
            f"🌡️ Температура воздуха: {weather_data.get('Температура воздуха, °C', 'Нет данных')} °C\n"
            f"🌬️ Средняя скорость ветра: {weather_data.get('Средняя скорость ветра, м/с', 'Нет данных')} м/с\n"
            f"➡️ Направление ветра: {weather_data.get('Направление ветра', 'Нет данных')}\n"
            f"🔽 Атмосферное давление: {weather_data.get('Атмосферное давление на уровне станции, мм рт.ст.', 'Нет данных')} мм рт.ст.\n"
            f"💧 Относительная влажность: {weather_data.get('Относительная влажность, %', 'Нет данных')} %\n"
            f"🌫️ Горизонтальная видимость: {weather_data.get('Горизонтальная видимость, км', 'Нет данных')} км\n"
            f"☁️ Балл общей облачности: {weather_data.get('Балл общей облачности', 'Нет данных')}\n"
            f"🌨️ Осадки за 12 часов: {weather_data.get('Осадки за 12 часов, мм', 'Нет данных')} мм\n"
            f"❄️ Высота снежного покрова: {weather_data.get('Высота снежного покрова, см', 'Нет данных')} см\n"
            "Данные предоставлены Гидрометцентром России"
        )
        await msg.answer(message_text)
    except Exception as e:
        await msg.answer(f"Ошибка при получении данных: {str(e)}")
    finally:
        clear_user_handlers(msg.from_id)


# Guess temperature game
@bot.on.message(text=["🎮Угадать температуру", "/guess_temp"])
async def guess_temp_handler(message: Message):
    user_id = message.from_id
    
    if user_id in user_guess_temp_state:
        await message.answer("Вы уже участвуете в игре! Продолжайте угадывать.")
        return
    
    target_temp = random.randint(-30, 40)
    user_guess_temp_state[user_id] = {
        "target_temp": target_temp,
        "attempts": 0,
        "max_attempts": 5,
        "last_guess": time.time()
    }
    
    await message.answer(
        "🌡️ Я загадал температуру от -30°C до 40°C. Угадай её за 5 попыток!\n❓ Введи свою догадку:"
    )

    # Устанавливаем обработчик для угадывания температуры
    current_handlers[user_id] = process_guess_temp

async def process_guess_temp(message: Message):
    user_id = message.from_id
    if user_id not in user_guess_temp_state:
        await message.answer("Игра не начата. Введите /guess_temp, чтобы начать.")
        clear_user_handlers(user_id)
        return
    
    if message.text.lower() in ["отмена", "cancel"]:
        await message.answer("❌ Игра отменена.")
        del user_guess_temp_state[user_id]
        clear_user_handlers(user_id)
        return
    
    try:
        guess = int(message.text)
        state = user_guess_temp_state[user_id]
        state["attempts"] += 1
        state["last_guess"] = time.time()
        
        if guess == state["target_temp"]:
            await message.answer(
                f"🎉 Поздравляю! Это {state['target_temp']}°C. Ты угадал за {state['attempts']} попыток!"
            )
            del user_guess_temp_state[user_id]
            clear_user_handlers(user_id)
        elif state["attempts"] >= state["max_attempts"]:
            await message.answer(
                f"😔 Попытки закончились. Загаданная температура была {state['target_temp']}°C."
            )
            del user_guess_temp_state[user_id]
            clear_user_handlers(user_id)
        else:
            difference = abs(state["target_temp"] - guess)
            if difference > 20:
                hint = "❄️ Очень холодно!"
            elif difference > 10:
                hint = "🌬️ Холодно, но ближе!"
            elif difference > 5:
                hint = "🌤️ Тепло, но ещё можно ближе!"
            else:
                hint = "🔥 Горячо! Почти у цели!"
            await message.answer(
                f"{hint}\n❓ Попытка {state['attempts']}/{state['max_attempts']}: Введи новую догадку:"
            )
            
    except ValueError:
        await message.answer("⚠️ Пожалуйста, вводите целое число.")
        return

    current_handlers[user_id] = process_guess_temp
    process_guess_temp.once = True


# Statistics command (admin only)
@bot.on.message(text=["/stats"])
async def stats_handler(message: Message):
    if message.from_id != ADMIN_ID:
        await message.answer("🔒 У вас нет доступа к этой команде.")
        return
    
    try:
        if not os.path.exists(USER_STATS_FILE):
            await message.answer("📊 Статистика пока пуста.")
            return
            
        with open(USER_STATS_FILE, mode='r', encoding='utf-8') as file:
            reader = csv.reader(file)
            next(reader)  # Пропускаем заголовок
            stats_lines = list(reader)
            
        if not stats_lines:
            await message.answer("📊 Статистика пока пуста.")
            return
            
        # Подсчет уникальных пользователей
        unique_users = len({row[0] for row in stats_lines if len(row) > 0})
        
        # Подсчет популярных команд
        from collections import Counter
        commands = Counter(row[2] for row in stats_lines if len(row) > 2)
        top_commands = "\n".join(f"{cmd}: {count}" for cmd, count in commands.most_common(5))
        
        stats_message = (
            f"📊 Общая статистика:\n"
            f"👥 Уникальных пользователей: {unique_users}\n"
            f"📝 Всего записей: {len(stats_lines)}\n\n"
            f"🔝 Топ-5 команд:\n{top_commands}\n\n"
            f"Последние 5 записей:\n"
        )
        
        # Добавляем последние записи
        for row in stats_lines[-5:]:
            if len(row) >= 4:
                stats_message += f"👤 {row[1]} ({row[0]})\n🕒 {row[3]}\n📝 {row[2]}\n───────────────\n"
                
        await message.answer(stats_message)
        
    except Exception as e:
        await message.answer(f"⚠️ Ошибка чтения статистики: {str(e)}")


# Location command
@bot.on.message(text=["📍Определить локацию"])
async def location_handler(message: Message):
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("Отправить местоположение", payload={"cmd": "request_location"}))
    await message.answer("Нажмите кнопку ниже, чтобы отправить своё местоположение:", keyboard=keyboard)


@bot.on.raw_event(GroupEventType.MESSAGE_EVENT, MessageEvent, payload_contains={"cmd": "request_location"})
async def handle_location(event: MessageEvent):
    await event.answer("Пожалуйста, отправьте геопозицию через VK.")
    current_handlers[event.user_id] = lambda m: process_location(m)


async def process_location(message: Message):
    if not message.geo:
        await message.answer("⚠️ Не удалось получить координаты.")
        return
    lat, lon = message.geo.coordinates.latitude, message.geo.coordinates.longitude
    geocoder_params = {'key': api_key, 'q': f'{lat},{lon}'}
    data = await fetch_json(f'{weather_url}/search.json', params=geocoder_params)
    if not data:
        await message.answer("Не удалось определить город по координатам.")
        return
    city = data[0]['name']
    save_city(message.from_id, city)
    parameters = {'key': api_key, 'q': city}
    weather_data = await fetch_json(f'{weather_url}/current.json', params=parameters)
    if not weather_data:
        await message.answer("Не удалось получить погоду.")
        return
    loc = weather_data['location']['name'] + ', ' + weather_data['location']['country']
    temp_c = weather_data['current']['temp_c']
    await message.answer(f"📍 Местоположение определено: {loc}\n🌡️ Температура: {temp_c}°C")

# Guess temperature game
@bot.on.message(text=["🎮Угадать температуру", "/guess_temp"])
async def guess_temp_handler(message: Message):
    user_id = message.from_id
    
    if user_id in user_guess_temp_state:
        await message.answer("Вы уже участвуете в игре! Продолжайте угадывать.")
        return
    
    target_temp = random.randint(-30, 40)
    user_guess_temp_state[user_id] = {
        "target_temp": target_temp,
        "attempts": 0,
        "max_attempts": 5,
        "last_guess": time.time()
    }
    
    keyboard = await get_main_keyboard(message.peer_id)
    await message.answer(
        "🌡️ Я загадал температуру от -30°C до 40°C. Угадай её за 5 попыток!\n❓ Введи свою догадку:",
        keyboard=keyboard
    )

    # Устанавливаем обработчик для угадывания температуры
    async def process_guess_temp(msg: Message):
        # Проверяем что это сообщение от нужного пользователя
        if msg.from_id != user_id:
            return
            
        if user_id not in user_guess_temp_state:
            await msg.answer("Игра не начата. Введите /guess_temp, чтобы начать.")
            clear_user_handlers(user_id)
            return
        
        if msg.text.lower() in ["отмена", "cancel"]:
            await msg.answer("❌ Игра отменена.", keyboard=await get_main_keyboard(msg.peer_id))
            del user_guess_temp_state[user_id]
            clear_user_handlers(user_id)
            return
        
        try:
            guess = int(msg.text)
            state = user_guess_temp_state[user_id]
            state["attempts"] += 1
            state["last_guess"] = time.time()
            
            if guess == state["target_temp"]:
                await msg.answer(
                    f"🎉 Поздравляю! Это {state['target_temp']}°C. Ты угадал за {state['attempts']} попыток!",
                    keyboard=await get_main_keyboard(msg.peer_id)
                )
                del user_guess_temp_state[user_id]
                clear_user_handlers(user_id)
            elif state["attempts"] >= state["max_attempts"]:
                await msg.answer(
                    f"😔 Попытки закончились. Загаданная температура была {state['target_temp']}°C.",
                    keyboard=await get_main_keyboard(msg.peer_id)
                )
                del user_guess_temp_state[user_id]
                clear_user_handlers(user_id)
            else:
                difference = abs(state["target_temp"] - guess)
                if difference > 20:
                    hint = "❄️ Очень холодно!"
                elif difference > 10:
                    hint = "🌬️ Холодно, но ближе!"
                elif difference > 5:
                    hint = "🌤️ Тепло, но ещё можно ближе!"
                else:
                    hint = "🔥 Горячо! Почти у цели!"
                await msg.answer(
                    f"{hint}\n❓ Попытка {state['attempts']}/{state['max_attempts']}: Введи новую догадку:"
                )
                
        except ValueError:
            await msg.answer("⚠️ Пожалуйста, вводите целое число.")

    process_guess_temp.once = False  # Не удаляем автоматически, так как игра многоходовая
    current_handlers[user_id] = process_guess_temp


# Run bot
async def start_bot():
    while True:
        try:
            await bot.run_forever()
        except Exception as e:
            print(f"Ошибка: {e}. Переподключаемся...")
            await asyncio.sleep(5)
