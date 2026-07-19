"""Структуры данных заявки: результат разбора текста, категории, интенты."""

from enum import Enum

from pydantic import BaseModel, ConfigDict


class Intent(str, Enum):
    """Что хочет пользователь: заявка, напоминание или другое сообщение."""

    new_order = "new_order"
    reminder = "reminder"
    other = "other"


class Category(str, Enum):
    """Категории услуг; добавление новых описано в docs/extending.md."""

    repair = "ремонт"
    transport = "перевозки"
    furniture = "сборка мебели"
    electrics = "электрика"
    plumbing = "сантехника"
    other = "прочее"


class ParsedOrder(BaseModel):
    """Разобранная заявка из свободного текста или голосового сообщения."""

    client_name: str | None = None
    phone: str | None = None
    org: str | None = None
    address: str | None = None
    # None = категория из текста не понятна, бот задаст уточняющий вопрос
    category: Category | None = None
    problem: str
    deadline: str | None = None  # ISO-дата или None
    income_rub: float | None = None
    expense_rub: float | None = None
    urgency: str | None = None
    comment: str | None = None
    intent: Intent = Intent.new_order
    existing_order_change: bool = False

    @property
    def profit_rub(self) -> float | None:
        """Прибыль = доход - расход; None, если данных недостаточно."""
        if self.income_rub is None:
            return None
        return self.income_rub - (self.expense_rub or 0)


class LlmParsedOrder(BaseModel):
    """Схема ответа YandexGPT в режиме structured output.

    Сервер требует, чтобы в JSON-схеме ВСЕ поля были required, поэтому
    здесь ни у одного поля нет дефолта: необязательность выражена через
    nullable-типы (модель обязана явно вернуть null). Доменная модель
    ParsedOrder с дефолтами остаётся отдельно — эта схема только для LLM.
    """

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    client_name: str | None
    phone: str | None
    org: str | None
    address: str | None
    category: Category | None
    problem: str
    deadline: str | None
    income_rub: float | None
    expense_rub: float | None
    urgency: str | None
    comment: str | None
    existing_order_change: bool


class LlmParsedOrders(BaseModel):
    """Обёртка structured output для одной или нескольких заявок."""

    model_config = ConfigDict(extra="forbid")

    orders: list[LlmParsedOrder]
