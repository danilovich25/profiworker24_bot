import sqlite3
from datetime import datetime


DB_NAME = "profiworker24.db"


# Создание базы
def create_database():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        name TEXT,
        organization TEXT,
        phone TEXT,
        service TEXT,
        status TEXT,
        income REAL,
        comment TEXT
    )
    """)

    conn.commit()
    conn.close()


# Сохранение заявки
def save_order(name, organization, phone, service, status, income, comment):

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    date = datetime.now().strftime("%d.%m.%Y")

    cursor.execute("""
    INSERT INTO orders
    (date, name, organization, phone, service, status, income, comment)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
        date,
        name,
        organization,
        phone,
        service,
        status,
        income,
        comment
    ))

    conn.commit()
    conn.close()


# Поиск заявки по телефону
def find_order(phone):

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT * FROM orders
    WHERE phone = ?
    ORDER BY id DESC
    """, (phone,))

    result = cursor.fetchall()

    conn.close()

    return result


# Получение всех заявок
def get_all_orders():

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT * FROM orders
    ORDER BY id DESC
    """)

    result = cursor.fetchall()

    conn.close()

    return result
