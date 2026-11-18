import os
import re
import asyncio
import time
import requests
import traceback
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from yandex_music import Client, Playlist
from aiohttp import web

# Загружаем переменные окружения из файла .env
load_dotenv()

# Безопасно считываем токены
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("❌ Критическая ошибка: не найден TELEGRAM_BOT_TOKEN в файле .env!")
if not YANDEX_TOKEN:
    raise ValueError("❌ Критическая ошибка: не найден YANDEX_TOKEN в файле .env!")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

ym_client = Client(YANDEX_TOKEN).init()

# Хранилище сессий плейлистов/альбомов для каждого пользователя
user_playlists = {}

# Кэш Telegram file_id (track_id -> file_id). Ускоряет повторную отправку треков до нуля!
track_file_cache = {}

# Семафор для ограничения одновременных потоков загрузки (не больше 3 одновременно)
download_semaphore = asyncio.Semaphore(3)

# Регулярные выражения для всех типов ссылок
TRACK_RE = re.compile(r"track/(\d+)")
ALBUM_RE = re.compile(r"album/(\d+)")
USER_PLAYLIST_RE = re.compile(r"users/([^/]+)/playlists/(\d+)")
PUBLIC_PLAYLIST_UUID_RE = re.compile(r"playlists/([0-9a-fA-F-]+)")


def download_track_obj(track, path: str) -> dict:
    """Скачивает трек в максимальном качестве (320 kbps или fallback на 192)"""
    try:
        track.download(path, codec="mp3", bitrate_in_kbps=320)
    except Exception:
        track.download(path, codec="mp3", bitrate_in_kbps=192)

    artists = ", ".join(a.name for a in track.artists) if track.artists else "Яндекс Музыка"
    return {
        "title": track.title,
        "artist": artists,
    }


def get_playlist_keyboard(tracks: list, page: int = 0) -> InlineKeyboardMarkup:
    """Генерирует клавиатуру пагинации по 10 треков на страницу + кнопку скачивания всего плейлиста"""
    builder = InlineKeyboardBuilder()

    page_size = 10
    total_pages = (len(tracks) + page_size - 1) // page_size

    start_idx = page * page_size
    end_idx = min(start_idx + page_size, len(tracks))
    page_tracks = tracks[start_idx:end_idx]

    for i, track in enumerate(page_tracks):
        global_idx = start_idx + i + 1
        artists = ", ".join(a.name for a in track.artists) if track.artists else "Музыкант"
        btn_text = f"{global_idx}. {track.title} - {artists}"

        if len(btn_text) > 50:
            btn_text = btn_text[:47] + "..."

        builder.button(text=btn_text, callback_data=f"dl:{global_idx - 1}")

    builder.adjust(1)

    # Кнопка скачивания всего плейлиста целиком
    builder.row(InlineKeyboardButton(text="📥 Скачать весь плейлист целиком", callback_data="dl_all"))

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page:{page - 1}"))

    nav_buttons.append(InlineKeyboardButton(text=f"📄 {page + 1} / {total_pages}", callback_data="noop"))

    if end_idx < len(tracks):
        nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"page:{page + 1}"))

    if nav_buttons:
        builder.row(*nav_buttons)

    return builder.as_markup()


@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот для скачивания музыки из Яндекс Музыки в высоком качестве (320 kbps).\n\n"
        "🔗 Отправь мне ссылку на трек, альбом или плейлист (можно из мобильного приложения через кнопку «Поделиться»)."
    )


@dp.message(F.text.contains("music.yandex"))
async def handle_yandex_link(message: types.Message):
    url = message.text.strip()
    status_msg = await message.answer("🔍 Распознаю ссылку...")

    try:
        # 1. Проверка на отдельный трек
        track_match = TRACK_RE.search(url)
        if track_match:
            track_id = track_match.group(1)
            await status_msg.edit_text("🔍 Скачиваю трек в высоком качестве...")

            # Проверяем кэш file_id для отдельного трека
            t_start = time.time()
            if int(track_id) in track_file_cache:
                t_cache_start = time.time()
                await message.answer_audio(
                    audio=track_file_cache[int(track_id)],
                    title="Трек",
                    performer="Яндекс Музыка"
                )
                print(f"[Кэш] Отдельный трек отправлен за {time.time() - t_cache_start:.2f} сек")
                await status_msg.delete()
                return

            tracks_list = await asyncio.to_thread(ym_client.tracks, [track_id])
            if not tracks_list:
                await status_msg.edit_text("⚠️ Трек не найден или недоступен.")
                return

            track_obj = tracks_list[0]
            path = f"track_{track_id}.mp3"

            t_dl = time.time()
            meta = await asyncio.to_thread(download_track_obj, track_obj, path)
            print(f"[Загрузка с Яндекса] Трек скачан за {time.time() - t_dl:.2f} сек")

            await status_msg.edit_text("📤 Отправляю музыку...")
            t_up = time.time()
            msg = await message.answer_audio(
                audio=FSInputFile(path),
                title=meta["title"],
                performer=meta["artist"],
            )
            print(f"[Загрузка в Telegram] Трек отправлен за {time.time() - t_up:.2f} сек")
            print(f"Всего обработка трека заняла: {time.time() - t_start:.2f} сек\n---")

            if msg.audio:
                track_file_cache[track_obj.id] = msg.audio.file_id

            await status_msg.delete()
            if os.path.exists(path):
                os.remove(path)
            return

        # 2. Обработка сборников (плейлисты и альбомы)
        tracks = []
        playlist_title = "Плейлист"
        playlist = None

        user_pl_match = USER_PLAYLIST_RE.search(url)
        if user_pl_match:
            username, kind = user_pl_match.groups()
            playlist = await asyncio.to_thread(ym_client.users_playlists, kind, username)

        elif PUBLIC_PLAYLIST_UUID_RE.search(url):
            uuid = PUBLIC_PLAYLIST_UUID_RE.search(url).group(1)

            def resolve_mobile_playlist():
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                try:
                    resp = requests.get(f"https://music.yandex.ru/playlists/{uuid}", headers=headers,
                                        allow_redirects=True, timeout=5)
                    m = USER_PLAYLIST_RE.search(resp.url)
                    if m:
                        u, k = m.groups()
                        return ym_client.users_playlists(k, u)
                    m2 = re.search(r"users/([^/\"']+)/playlists/(\d+)", resp.text)
                    if m2:
                        u, k = m2.groups()
                        return ym_client.users_playlists(k, u)
                    owner_match = re.search(r'"owner"\s*:\s*\{[^}]*"login"\s*:\s*"([^"]+)"', resp.text)
                    kind_match = re.search(r'"kind"\s*:\s*(\d+)', resp.text)
                    if owner_match and kind_match:
                        return ym_client.users_playlists(kind_match.group(1), owner_match.group(1))
                except Exception as ex:
                    print(f"Ошибка авто-распознавания UUID: {ex}")
                return None

            playlist = await asyncio.to_thread(resolve_mobile_playlist)

        elif ALBUM_RE.search(url):
            album_id = ALBUM_RE.search(url).group(1)
            album = await asyncio.to_thread(ym_client.albums_with_tracks, album_id)
            if album:
                playlist_title = album.title
                if album.volumes:
                    for vol in album.volumes:
                        tracks.extend(vol)

        if playlist:
            playlist_title = playlist.title
            if hasattr(playlist, "fetch_tracks") and (not playlist.tracks or len(playlist.tracks) == 0):
                await asyncio.to_thread(playlist.fetch_tracks)

            if playlist.tracks:
                tracks = [item.track for item in playlist.tracks if item.track]

        if not tracks:
            await status_msg.edit_text("⚠️ Не удалось найти треки по этой ссылке или плейлист пуст.")
            return

        user_playlists[message.from_user.id] = tracks
        await status_msg.edit_text(
            f"🎵 Плейлист/Альбом: {playlist_title}\n"
            f"Всего треков: {len(tracks)}.\n"
            "Выбери нужный трек или скачай весь плейлист целиком кнопками ниже:",
            reply_markup=get_playlist_keyboard(tracks, page=0),
        )

    except Exception:
        traceback.print_exc()
        await status_msg.edit_text(
            "⚠️ Произошла ошибка при обработке ссылки. Проверьте правильность и попробуйте еще раз.")


@dp.callback_query(F.data.startswith("page:"))
async def process_pagination(callback: types.CallbackQuery):
    page = int(callback.data.split(":")[1])
    tracks = user_playlists.get(callback.from_user.id)

    if not tracks:
        await callback.answer("⚠️ Сессия устарела. Отправьте ссылку заново.", show_alert=True)
        return

    await callback.message.edit_reply_markup(
        reply_markup=get_playlist_keyboard(tracks, page=page)
    )
    await callback.answer()


@dp.callback_query(F.data == "dl_all")
async def process_download_all(callback: types.CallbackQuery):
    tracks = user_playlists.get(callback.from_user.id)
    if not tracks:
        await callback.answer("⚠️ Сессия устарела. Отправьте ссылку заново.", show_alert=True)
        return

    await callback.answer("📥 Начинаю ускоренное скачивание плейлиста...")
    status_msg = await callback.message.answer(f"⏳ Параллельная подготовка (0 / {len(tracks)})...")

    success_count = 0
    completed = 0
    lock = asyncio.Lock()

    async def download_and_send_worker(track):
        nonlocal success_count, completed
        async with download_semaphore:
            t_start = time.time()
            track_id = track.id
            path = f"track_{track_id}.mp3"
            chat_id = callback.message.chat.id
            artists = ", ".join(a.name for a in track.artists) if track.artists else "Яндекс Музыка"

            try:
                # 1. Проверка кэша file_id
                if track_id in track_file_cache:
                    t_cache = time.time()
                    msg = await bot.send_audio(
                        chat_id=chat_id,
                        audio=track_file_cache[track_id],
                        title=track.title,
                        performer=artists,
                    )
                    print(f"[Кэш] Трек '{track.title}' отправлен за {time.time() - t_cache:.2f} сек")
                else:
                    # 2. Скачивание с Яндекса
                    t_dl = time.time()
                    meta = await asyncio.to_thread(download_track_obj, track, path)
                    print(f"[Яндекс] '{track.title}' скачан за {time.time() - t_dl:.2f} сек")

                    # 3. Отправка в Telegram
                    t_up = time.time()
                    msg = await bot.send_audio(
                        chat_id=chat_id,
                        audio=FSInputFile(path),
                        title=meta["title"],
                        performer=meta["artist"],
                    )
                    print(f"[Telegram] '{track.title}' загружен за {time.time() - t_up:.2f} сек")

                    if msg.audio:
                        track_file_cache[track_id] = msg.audio.file_id

                print(f"Всего на трек '{track.title}' ушло {time.time() - t_start:.2f} сек\n---")

                async with lock:
                    success_count += 1
                    completed += 1
                    await status_msg.edit_text(f"⏳ Скачивание плейлиста: выполнено {completed} из {len(tracks)}")

            except Exception as e:
                print(f"Ошибка при обработке трека {track.title}: {e}")
                async with lock:
                    completed += 1
            finally:
                if os.path.exists(path):
                    os.remove(path)

    # Запускаем все задачи параллельно, семафор автоматически ограничит их до 3 одновременно
    tasks = [download_and_send_worker(track) for track in tracks]
    await asyncio.gather(*tasks)

    await status_msg.edit_text(f"✅ Готово! Успешно отправлено треков: {success_count} из {len(tracks)}.")


@dp.callback_query(F.data.startswith("dl:"))
async def process_download_track(callback: types.CallbackQuery):
    index = int(callback.data.split(":")[1])
    tracks = user_playlists.get(callback.from_user.id)

    if not tracks or index >= len(tracks):
        await callback.answer("⚠️ Трек не найден в текущей сессии.", show_alert=True)
        return

    track = tracks[index]
    track_id = track.id
    path = f"track_{track_id}.mp3"

    await callback.answer(f"⬇️ Скачиваю: {track.title}")
    status_msg = await callback.message.answer(f"🔍 Скачиваю трек №{index + 1}: {track.title}...")

    try:
        t_start = time.time()
        # Проверяем кэш
        if track_id in track_file_cache:
            t_cache = time.time()
            await callback.message.answer_audio(
                audio=track_file_cache[track_id],
                title=track.title,
                performer=", ".join(a.name for a in track.artists) if track.artists else "Яндекс Музыка",
            )
            print(f"[Кэш] Трек по клику отправлен за {time.time() - t_cache:.2f} сек")
            await status_msg.delete()
            return

        meta = await asyncio.to_thread(download_track_obj, track, path)
        print(f"[Яндекс] Трек по клику скачан за {time.time() - t_start:.2f} сек")

        await status_msg.edit_text("📤 Отправляю музыку...")
        t_up = time.time()
        msg = await callback.message.answer_audio(
            audio=FSInputFile(path),
            title=meta["title"],
            performer=meta["artist"],
        )
        print(f"[Telegram] Трек по клику отправлен за {time.time() - t_up:.2f} сек")

        if msg.audio:
            track_file_cache[track_id] = msg.audio.file_id

        await status_msg.delete()
    except Exception:
        traceback.print_exc()
        await status_msg.edit_text("⚠️ Ошибка при скачивании трека.")
    finally:
        if os.path.exists(path):
            os.remove(path)


@dp.callback_query(F.data == "noop")
async def process_noop(callback: types.CallbackQuery):
    await callback.answer()


# Веб-сервер заглушка для Render (чтобы открывать порт и получать статус Live)
async def handle_ping(request):
    return web.Response(text="Bot is alive")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    # 1. Сначала запускаем веб-сервер, чтобы открыть порт для Render
    await start_dummy_server()
    
    print("Бот успешно запущен и готов к работе со скоростным кэшем и параллельной загрузкой!")
    
    # 2. Запуск поллинга бота
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
