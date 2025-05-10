import os
import time
import sqlite3
import logging
import requests
import webbrowser
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from flask import Flask, render_template_string, request, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR

# === НАСТРОЙКИ ===
PROFILE_ID = 'kxrosvb'  # Замените на ваш AdsPower ID
SUBSCRIBER_URL = 'https://onlyfans.com/my/notifications/subscribed'
PURCHASES_URL = 'https://onlyfans.com/my/notifications/purchases'
TIPS_URL = 'https://onlyfans.com/my/notifications/tip'
DB_NAME = 'onlyfans_data.db'
ADSPOWER_API = 'http://localhost:50325/api/v1/browser/start'
LOG_FILE = 'onlyfans_collector.log'

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
scheduler = BackgroundScheduler()

# === HTML ШАБЛОН ===
HTML_TEMPLATE = '''
<!doctype html>
<html lang="en">
  <head>
    <title>OnlyFans Dashboard</title>
    <style>
      body { font-family: Arial, sans-serif; margin: 20px; }
      table { border-collapse: collapse; width: 100%; margin-bottom: 30px; }
      th, td { border: 1px solid #ccc; padding: 8px; }
      th { background: #f4f4f4; }
      form { margin-bottom: 20px; }
      .btn { background: #007BFF; color: white; padding: 6px 12px; border: none; cursor: pointer; }
      .btn:hover { background: #0056b3; }
      .status { padding: 10px; margin: 10px 0; border-radius: 4px; }
      .success { background: #d4edda; color: #155724; }
      .error { background: #f8d7da; color: #721c24; }
    </style>
  </head>
  <body>
    <h1>📊 OnlyFans Dashboard</h1>
    
    {% if message %}
    <div class="status {{ message.type }}">{{ message.text }}</div>
    {% endif %}

    <form method="get">
      <input type="text" name="username" placeholder="Имя пользователя" value="{{ filters.username }}">
      <select name="type">
        <option value="">Тип транзакции</option>
        <option value="purchase" {% if filters.type == 'purchase' %}selected{% endif %}>Покупка</option>
        <option value="tip" {% if filters.type == 'tip' %}selected{% endif %}>Чаевые</option>
      </select>
      <input type="date" name="date" value="{{ filters.date }}">
      <button class="btn" type="submit">Фильтр</button>
      <a href="{{ url_for('update') }}" class="btn">🔄 Обновить данные</a>
      <a href="{{ url_for('view_logs') }}" class="btn">📋 Показать логи</a>
    </form>

    <h2>👥 Подписчики (3 дня)</h2>
    {% if subscribers %}
    <table>
      <tr><th>Username</th><th>Дата подписки</th></tr>
      {% for s in subscribers %}
        <tr><td>{{ s[0] }}</td><td>{{ s[1] }}</td></tr>
      {% endfor %}
    </table>
    {% else %}
    <p>Нет данных о подписчиках за последние 3 дня</p>
    {% endif %}

    <h2>💰 Финансовые транзакции</h2>
    {% if transactions %}
    <table>
      <tr><th>Username</th><th>Тип</th><th>Сумма</th><th>Дата</th></tr>
      {% for t in transactions %}
        <tr><td>{{ t[0] }}</td><td>{{ t[1] }}</td><td>{{ t[2] }}</td><td>{{ t[3] }}</td></tr>
      {% endfor %}
    </table>
    {% else %}
    <p>Нет данных о транзакциях</p>
    {% endif %}
  </body>
</html>
'''

def init_db():
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        # Создаем таблицы, если их нет
        c.execute('''CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY, 
            username TEXT UNIQUE, 
            subscribed_date TEXT, 
            collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY, 
            username TEXT, 
            amount REAL, 
            type TEXT, 
            date TEXT, 
            collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Очищаем старые/некорректные данные
        c.execute("DELETE FROM subscribers WHERE username IN ('notifications', 'settings', 'help', 'create', 'statistics')")
        c.execute("DELETE FROM subscribers WHERE LENGTH(username) < 3 OR username LIKE 'u%'")
        c.execute("DELETE FROM subscribers WHERE subscribed_date < ?", 
                 ((datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'),))
        
        conn.commit()
        return conn
        
    except sqlite3.Error as e:
        logger.error(f"Ошибка при инициализации БД: {e}")
        raise

def check_adspower_running():
    try:
        response = requests.get(ADSPOWER_API, timeout=5)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False

def start_adspower_browser(profile_id):
    if not check_adspower_running():
        raise Exception("AdsPower не запущен или API недоступно")
    
    try:
        resp = requests.get(f'{ADSPOWER_API}?user_id={profile_id}', timeout=10)
        data = resp.json()
        
        if data.get('code') != 0:
            raise Exception(f"Ошибка AdsPower: {data.get('msg', 'Неизвестная ошибка')}")
        
        path = data['data']['webdriver']
        debugger = data['data']['ws']['selenium']
        
        options = webdriver.ChromeOptions()
        options.debugger_address = debugger
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-infobars')
        options.add_argument('--disable-notifications')
        
        service = Service(path)
        driver = webdriver.Chrome(service=service, options=options)
        
        # Проверка авторизации в OnlyFans
        driver.get('https://onlyfans.com')
        try:
            WebDriverWait(driver, 10).until(
                lambda d: 'auth' not in d.current_url and 'login' not in d.current_url)
        except TimeoutException:
            raise Exception("Требуется авторизация в OnlyFans")
        
        return driver
        
    except Exception as e:
        logger.error(f"Ошибка при запуске браузера AdsPower: {e}")
        raise

def scroll_page(driver):
    last_height = driver.execute_script("return document.body.scrollHeight")
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

def collect_subscribers(driver, db):
    try:
        logger.info("Открываем страницу подписчиков...")
        driver.get(SUBSCRIBER_URL)
        time.sleep(5)  # Даем странице полностью загрузиться
        
        # Ждем появления хотя бы одного элемента
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'div.table-item'))
            logger.info("Элементы страницы загружены")
        except TimeoutException:
            logger.error("Не удалось загрузить элементы подписчиков")
            return

        # Прокрутка страницы
        scroll_page(driver)
        
        # Улучшенный селектор с учетом структуры OnlyFans 2024
        subscriber_blocks = driver.find_elements(By.CSS_SELECTOR, 'div.table-item:has(a[href^="/"][href*="/profile/"])')
        logger.info(f"Найдено {len(subscriber_blocks)} блоков с подписчиками")
        
        cur = db.cursor()
        new_subscribers = 0
        
        for block in subscriber_blocks:
            try:
                # Получаем ссылку на профиль
                profile_link = block.find_element(By.CSS_SELECTOR, 'a[href^="/"][href*="/profile/"]')
                href = profile_link.get_attribute("href")
                username = href.split("/")[-1]
                
                # Расширенная фильтрация
                system_usernames = {
                    'notifications', 'settings', 'help', 'create', 'collections',
                    'vault', 'queue', 'statistics', 'active', 'earnings',
                    'dashboard', 'subscribed', 'purchases', 'tags', 'commented',
                    'mentioned', 'favorited', 'message', 'onlyfans'
                }
                
                if (not username or len(username) < 3 or 
                    username in system_usernames or
                    username.startswith('u') or
                    username.isdigit()):
                    continue
                
                # Проверяем, есть ли имя пользователя в блоке (дополнительная проверка)
                try:
                    username_element = block.find_element(By.CSS_SELECTOR, 'div.user-info span.name')
                    if not username_element.text.strip():
                        continue
                except NoSuchElementException:
                    continue
                
                # Проверка на дубликаты
                cur.execute("SELECT 1 FROM subscribers WHERE username = ?", (username,))
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO subscribers (username, subscribed_date) VALUES (?, ?)",
                        (username, datetime.now().strftime('%Y-%m-%d')))
                    new_subscribers += 1
                    logger.debug(f"Добавлен подписчик: {username}")
                    
            except Exception as e:
                logger.warning(f"Ошибка при обработке блока подписчика: {str(e)}")
                continue
        
        logger.info(f"Успешно добавлено {new_subscribers} новых подписчиков")
        db.commit()
        
    except Exception as e:
        logger.error(f"Критическая ошибка при сборе подписчиков: {str(e)}")
        raise


def collect_transactions(driver, db, url, tx_type):
    try:
        logger.info(f"Открываем страницу транзакций ({tx_type})...")
        driver.get(url)
        time.sleep(5)
        
        # Ожидаем загрузки элементов
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'div.table-item')))
            logger.info("Элементы транзакций загружены")
        except TimeoutException:
            logger.error(f"Не удалось загрузить элементы транзакций ({tx_type})")
            return

        scroll_page(driver)
        
        # Улучшенный селектор для транзакций
        transaction_blocks = driver.find_elements(By.CSS_SELECTOR, 'div.table-item:has(div.amount)')
        logger.info(f"Найдено {len(transaction_blocks)} транзакций типа {tx_type}")
        
        cur = db.cursor()
        new_transactions = 0
        
        for block in transaction_blocks:
            try:
                # Получаем username
                username_element = block.find_element(By.CSS_SELECTOR, 'a[href^="/"][href*="/profile/"]')
                username = username_element.get_attribute("href").split("/")[-1]
                
                # Получаем сумму
                amount_element = block.find_element(By.CSS_SELECTOR, 'div.amount')
                amount_text = amount_element.text
                amount = float(amount_text.replace('$', '').replace(',', '').strip())
                
                # Получаем дату (примерная реализация)
                date_element = block.find_element(By.CSS_SELECTOR, 'div.date')
                date = datetime.now().strftime('%Y-%m-%d')  # Можно парсить из date_element.text
                
                # Проверка на дубликаты
                cur.execute("""SELECT 1 FROM transactions 
                            WHERE username=? AND amount=? AND type=? AND date=?""",
                          (username, amount, tx_type, date))
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO transactions (username, amount, type, date) VALUES (?, ?, ?, ?)",
                        (username, amount, tx_type, date))
                    new_transactions += 1
                    logger.debug(f"Добавлена транзакция: {username} - {amount} - {tx_type}")
                    
            except Exception as e:
                logger.warning(f"Ошибка при обработке транзакции: {str(e)}")
                continue
        
        logger.info(f"Успешно добавлено {new_transactions} новых транзакций типа {tx_type}")
        db.commit()
        
    except Exception as e:
        logger.error(f"Критическая ошибка при сборе транзакций ({tx_type}): {str(e)}")
        raise

def run_collector():
    logger.info("🔄 Запуск сбора данных...")
    conn = None
    driver = None
    
    try:
        conn = init_db()
        driver = start_adspower_browser(PROFILE_ID)
        
        collect_subscribers(driver, conn)
        collect_transactions(driver, conn, PURCHASES_URL, 'purchase')
        collect_transactions(driver, conn, TIPS_URL, 'tip')
        
        logger.info("✅ Сбор данных успешно завершен")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка при сборе данных: {e}")
        return False
        
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
        if conn:
            conn.close()

@app.route('/')
def dashboard():
    message = request.args.get('message')
    message_type = request.args.get('message_type')
    
    username = request.args.get('username', '').strip()
    tx_type = request.args.get('type', '')
    date = request.args.get('date', '')
    
    filters = {'username': username, 'type': tx_type, 'date': date}
    message_obj = {'text': message, 'type': message_type} if message else None
    
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()

        # Получаем подписчиков за последние 3 дня
        cur.execute(
            """SELECT username, subscribed_date FROM subscribers 
            WHERE subscribed_date >= ? 
            ORDER BY subscribed_date DESC""",
            ((datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d'),))
        subscribers = cur.fetchall()

        # Получаем транзакции с фильтрами
        query = """SELECT username, type, amount, date FROM transactions 
                WHERE 1=1"""
        args = []
        
        if username:
            query += " AND username LIKE ?"
            args.append(f"%{username}%")
        if tx_type:
            query += " AND type = ?"
            args.append(tx_type)
        if date:
            query += " AND date = ?"
            args.append(date)
            
        query += " ORDER BY date DESC"
        cur.execute(query, args)
        transactions = cur.fetchall()

        return render_template_string(
            HTML_TEMPLATE,
            subscribers=subscribers,
            transactions=transactions,
            filters=filters,
            message=message_obj)
            
    except Exception as e:
        logger.error(f"Ошибка при отображении dashboard: {e}")
        return render_template_string(
            HTML_TEMPLATE,
            subscribers=[],
            transactions=[],
            filters=filters,
            message={'text': f'Ошибка загрузки данных: {e}', 'type': 'error'})
            
    finally:
        if conn:
            conn.close()

@app.route('/update')
def update():
    success = run_collector()
    if success:
        return redirect(url_for('dashboard', 
                             message='Данные успешно обновлены', 
                             message_type='success'))
    else:
        return redirect(url_for('dashboard', 
                             message='Ошибка при обновлении данных', 
                             message_type='error'))

@app.route('/logs')
def view_logs():
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            logs = f.read()
        return f'<pre>{logs}</pre>'
    except Exception as e:
        return f'Ошибка при чтении логов: {e}'

def scheduler_error_listener(event):
    if event.exception:
        logger.error(f"Ошибка в запланированной задаче: {event.exception}")

if __name__ == '__main__':
    try:
        # Настройка планировщика (3 раза в день)
        scheduler.add_job(run_collector, 'cron', hour=8, id='morning_collection')
        scheduler.add_job(run_collector, 'cron', hour=16, id='afternoon_collection')
        scheduler.add_job(run_collector, 'cron', hour=23, id='evening_collection')
        scheduler.add_listener(scheduler_error_listener, EVENT_JOB_ERROR)
        scheduler.start()

        # Первоначальный сбор данных
        run_collector()

        # Открытие веб-интерфейса
        webbrowser.open("http://localhost:5000")

        # Запуск Flask-приложения
        app.run(port=5000)
        
    except Exception as e:
        logger.error(f"Фатальная ошибка при запуске приложения: {e}")
    finally:
        scheduler.shutdown()