import sqlite3
from datetime import datetime


DB_NAME = "profiworker24.db"


# Создание базы и таблицы
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

        income INTEGER,

        comment TEXT

    )
    """)

    conn.commit()
    conn.close()



# Сохранение заявки
def save_order(
        name,
        organization,
        phone,
        service,
        status,
        income,
        comment
):

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()


    cursor.execute("""
    INSERT INTO orders
    (
        date,
        name,
        organization,
        phone,
        service,
        status,
        income,
        comment
    )

    VALUES (?, ?, ?, ?, ?, ?, ?, ?)

    """,
    (
        datetime.now().strftime("%d.%m.%Y"),
        name or "-",
        organization or "-",
        phone or "-",
        service or "-",
        status or "Новая",
        income or 0,
        comment or "-"
    ))


    conn.commit()
    conn.close()



# Поиск по телефону
def search_by_phone(phone):

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()


    cursor.execute("""
    SELECT *
    FROM orders
    WHERE phone = ?
    ORDER BY id DESC
    """,
    (phone,))


    result = cursor.fetchall()

    conn.close()

    return result



# Поиск по имени или организации
def search_text(text):

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()


    cursor.execute("""
    SELECT *
    FROM orders
    WHERE name LIKE ?
    OR organization LIKE ?
    ORDER BY id DESC
    """,
    (
        f"%{text}%",
        f"%{text}%"
    ))


    result = cursor.fetchall()

    conn.close()

    return result



# Изменение статуса
def update_status(order_id, status):

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()


    cursor.execute("""
    UPDATE orders
    SET status = ?
    WHERE id = ?
    """,
    (
        status,
        order_id
    ))


    conn.commit()
    conn.close()



# Статистика
def get_statistics():

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()


    cursor.execute("""
    SELECT COUNT(*), SUM(income)
    FROM orders
    """)


    result = cursor.fetchone()

    conn.close()

    return result
