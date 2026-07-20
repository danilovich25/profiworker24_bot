"""Роутеры бота. Порядок подключения важен: команды и кнопки меню стоят
до разбора свободного текста, fallback идёт последним. Роутер правки заявок
стоит до поиска: его защитные хендлеры не дают /find и кнопкам меню молча
оборвать незаконченные правки."""

from app.handlers.edit import router as edit_router
from app.handlers.fallback import router as fallback_router
from app.handlers.messages import router as messages_router
from app.handlers.search import router as search_router
from app.handlers.start import router as start_router
from app.handlers.voice import router as voice_router

routers = (
    start_router,
    edit_router,
    search_router,
    voice_router,
    messages_router,
    fallback_router,
)
