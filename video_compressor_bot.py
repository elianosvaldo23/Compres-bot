import os
import time
import logging
import tempfile
import asyncio
import uuid
import shutil
import json
import re
from datetime import datetime
from functools import wraps
from typing import Optional, List, Dict, Any, Tuple

import ffmpeg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ConversationHandler
from telegram.constants import ParseMode, ChatAction
from telegram.error import TelegramError

# Configuración de logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuración del bot
TOKEN = "7551775190:AAFerA1RVjKl7L7CeD6kKZ3c5dAf9iK-ZJY"
MAX_FILE_SIZE = 1900 * 1024 * 1024  # 1.9GB - límite máximo para descargar archivos
MAX_UPLOAD_SIZE = 2000 * 1024 * 1024  # 2GB - límite máximo para subir a Telegram
TEMP_DOWNLOAD_DIR = "downloads"
TEMP_COMPRESSED_DIR = "compressed"
TEMP_SPLIT_DIR = "split_files"
TEMP_EXTRACT_DIR = "extracted"
PROFILES_DIR = "profiles"

# Estados para conversación
WAITING_TRIM_START, WAITING_TRIM_END = range(2)

# Diccionario para almacenar las tareas de compresión activas
active_tasks = {}

# Diccionario para almacenar las preferencias de los usuarios
user_preferences = {}

# Diccionario para almacenar estadísticas de uso
user_stats = {}

# Diccionario para almacenar mensajes originales
original_messages = {}

# Configuraciones predeterminadas de compresión
DEFAULT_COMPRESSION = {
    "preset": "medium",  # Opciones: ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow
    "crf": 23,  # Factor de tasa constante (0-51): menor valor = mejor calidad
    "audio_bitrate": "128k",  # Tasa de bits de audio
    "format": "mp4",  # Formato de salida
    "codec": "libx264",  # Códec de video
    "resolution": "original",  # Resolución: original, 1080p, 720p, 480p
    "speed": "1.0"  # Velocidad de reproducción
}

# Crear directorios temporales si no existen
for directory in [TEMP_DOWNLOAD_DIR, TEMP_COMPRESSED_DIR, TEMP_SPLIT_DIR, TEMP_EXTRACT_DIR, PROFILES_DIR]:
    os.makedirs(directory, exist_ok=True)

# Función para cargar datos de usuario
def load_user_data():
    global user_preferences, user_stats

    # Cargar preferencias de usuario
    prefs_file = os.path.join(PROFILES_DIR, "user_preferences.json")
    if os.path.exists(prefs_file):
        try:
            with open(prefs_file, 'r') as f:
                user_preferences = json.load(f)
        except Exception as e:
            logger.error(f"Error al cargar preferencias de usuario: {e}")

    # Cargar estadísticas de usuario
    stats_file = os.path.join(PROFILES_DIR, "user_stats.json")
    if os.path.exists(stats_file):
        try:
            with open(stats_file, 'r') as f:
                user_stats = json.load(f)
        except Exception as e:
            logger.error(f"Error al cargar estadísticas de usuario: {e}")

# Función para guardar datos de usuario
def save_user_data():
    # Guardar preferencias de usuario
    prefs_file = os.path.join(PROFILES_DIR, "user_preferences.json")
    try:
        with open(prefs_file, 'w') as f:
            json.dump(user_preferences, f)
    except Exception as e:
        logger.error(f"Error al guardar preferencias de usuario: {e}")

    # Guardar estadísticas de usuario
    stats_file = os.path.join(PROFILES_DIR, "user_stats.json")
    try:
        with open(stats_file, 'w') as f:
            json.dump(user_stats, f)
    except Exception as e:
        logger.error(f"Error al guardar preferencias de usuario: {e}")

# Cargar datos al inicio
load_user_data()

# Función para enviar acción de "typing" mientras se procesa
def send_action(action):
    def decorator(func):
        @wraps(func)
        async def command_func(update, context, *args, **kwargs):
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, 
                action=action
            )
            return await func(update, context, *args, **kwargs)
        return command_func
    return decorator

# Función para verificar el tamaño del archivo
async def check_file_size(update: Update, context: ContextTypes.DEFAULT_TYPE, file_size: int) -> bool:
    if file_size > MAX_FILE_SIZE:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ Lo siento, el archivo es demasiado grande. El tamaño máximo permitido es {MAX_FILE_SIZE/(1024*1024):.1f}MB.\n\n"
            f"Puedes:\n"
            f"1. Usar el comando /split para dividir el archivo en partes más pequeñas\n"
            f"2. Enviar un archivo más pequeño\n"
            f"3. Usar el comando /extract para extraer solo el audio"
        )
        return False
    return True

# Función para obtener las preferencias del usuario
def get_user_preferences(user_id: int) -> dict:
    user_id_str = str(user_id)
    if user_id_str not in user_preferences:
        user_preferences[user_id_str] = DEFAULT_COMPRESSION.copy()
    return user_preferences[user_id_str]

# Función para actualizar estadísticas de usuario
def update_user_stats(user_id: int, original_size: int, compressed_size: int):
    user_id_str = str(user_id)
    if user_id_str not in user_stats:
        user_stats[user_id_str] = {
            "total_files": 0,
            "total_original_size": 0,
            "total_compressed_size": 0,
            "space_saved": 0,
            "last_activity": datetime.now().isoformat()
        }

    user_stats[user_id_str]["total_files"] += 1
    user_stats[user_id_str]["total_original_size"] += original_size
    user_stats[user_id_str]["total_compressed_size"] += compressed_size
    user_stats[user_id_str]["space_saved"] += (original_size - compressed_size)
    user_stats[user_id_str]["last_activity"] = datetime.now().isoformat()

    # Guardar estadísticas
    save_user_data()

# Función para obtener información de un video
async def get_video_info(file_path: str) -> dict:
    try:
        probe = ffmpeg.probe(file_path)
        
        # Información general
        format_info = probe.get('format', {})
        duration = float(format_info.get('duration', 0))
        size = int(format_info.get('size', 0))
        bit_rate = int(format_info.get('bit_rate', 0)) if 'bit_rate' in format_info else 0
        
        # Información de video
        video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
        video_info = {}
        if video_stream:
            video_info = {
                'codec': video_stream.get('codec_name', 'unknown'),
                'width': int(video_stream.get('width', 0)),
                'height': int(video_stream.get('height', 0)),
                'fps': eval(video_stream.get('avg_frame_rate', '0/1')),
                'bit_rate': int(video_stream.get('bit_rate', 0)) if 'bit_rate' in video_stream else 0
            }
        
        # Información de audio
        audio_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'audio'), None)
        audio_info = {}
        if audio_stream:
            audio_info = {
                'codec': audio_stream.get('codec_name', 'unknown'),
                'channels': int(audio_stream.get('channels', 0)),
                'sample_rate': int(audio_stream.get('sample_rate', 0)),
                'bit_rate': int(audio_stream.get('bit_rate', 0)) if 'bit_rate' in audio_stream else 0
            }
        
        # Información de subtítulos
        subtitle_streams = [stream for stream in probe['streams'] if stream['codec_type'] == 'subtitle']
        subtitle_info = []
        for sub in subtitle_streams:
            subtitle_info.append({
                'codec': sub.get('codec_name', 'unknown'),
                'language': sub.get('tags', {}).get('language', 'unknown')
            })
        
        return {
            'format': format_info.get('format_name', 'unknown'),
            'duration': duration,
            'size': size,
            'bit_rate': bit_rate,
            'video': video_info,
            'audio': audio_info,
            'subtitles': subtitle_info
        }

    except Exception as e:
        logger.error(f"Error al obtener información del video: {e}")
        return {}

# Función para comprimir video
async def compress_video(input_file: str, output_file: str, preferences: dict, update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: str):
    try:
        # Obtener información del video original
        probe = ffmpeg.probe(input_file)
        video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
        
        if not video_stream:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ No se pudo encontrar una pista de video en el archivo."
            )
            return None
        
        # Obtener dimensiones originales
        width = int(video_stream['width'])
        height = int(video_stream['height'])
        
        # Ajustar resolución si es necesario
        if preferences["resolution"] != "original":
            if preferences["resolution"] == "1080p":
                if height > 1080:
                    # Calcular nueva anchura manteniendo la relación de aspecto
                    height = 1080
                    width = int(width * (1080 / int(video_stream['height'])))
            elif preferences["resolution"] == "720p":
                if height > 720:
                    height = 720
                    width = int(width * (720 / int(video_stream['height'])))
            elif preferences["resolution"] == "480p":
                if height > 480:
                    height = 480
                    width = int(width * (480 / int(video_stream['height'])))
            elif preferences["resolution"] == "360p":
                if height > 360:
                    height = 360
                    width = int(width * (360 / int(video_stream['height'])))
        
        # Asegurarse de que width y height sean pares (requerido por algunos códecs)
        width = width - (width % 2)
        height = height - (height % 2)
        
        # Configurar el proceso de compresión
        stream = ffmpeg.input(input_file)
        
        # Ajustar velocidad de reproducción si es diferente de 1.0
        speed = float(preferences.get("speed", "1.0"))
        if speed != 1.0:
            stream = ffmpeg.filter(stream, 'setpts', f'{1/speed}*PTS')
        
        # Actualizar estado a "procesando"
        active_tasks[task_id]["status"] = "processing"
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🔄 Procesando video...\n"
                f"📊 Configuración:\n"
                f"- Preset: {preferences['preset']}\n"
                f"- CRF: {preferences['crf']}\n"
                f"- Resolución: {preferences['resolution'] if preferences['resolution'] != 'original' else f'{width}x{height}'}\n"
                f"- Códec: {preferences['codec']}\n"
                f"- Audio: {preferences['audio_bitrate']}\n"
                f"- Velocidad: {preferences['speed']}x\n"
                f"⏳ Esto puede tomar varios minutos dependiendo del tamaño del video."
        )
        
        # Configurar opciones de video
        video_options = {
            'c:v': preferences['codec'],
            'preset': preferences['preset'],
            'crf': preferences['crf'],
            'width': width,
            'height': height
        }
        
        # Configurar opciones de audio
        audio_options = {
            'c:a': 'aac',
            'b:a': preferences['audio_bitrate']
        }
        
        # Combinar opciones
        output_options = {**video_options, **audio_options}
        
        # Iniciar el proceso de compresión
        process = (
            stream
            .output(output_file, **output_options)
            .global_args('-progress', 'pipe:1')
            .run_async(pipe_stdout=True, pipe_stderr=True)
        )
        
        # Monitorear el progreso
        last_update_time = time.time()
        progress_message = None
        
        # Obtener duración total del video
        total_duration = float(probe['format']['duration'])
        
        while True:
            if process.stdout:
                line = await process.stdout.readline()
                if not line:
                    break
                
                line_str = line.decode('utf-8', errors='ignore').strip()
                
                # Extraer información de progreso
                if 'out_time=' in line_str:
                    time_match = re.search(r'out_time=(\d+):(\d+):(\d+\.\d+)', line_str)
                    if time_match:
                        hours, minutes, seconds = map(float, time_match.groups())
                        current_time = hours * 3600 + minutes * 60 + seconds
                        progress_percent = min(100, int((current_time / total_duration) * 100))
                        
                        # Actualizar mensaje de progreso cada 5 segundos para no sobrecargar Telegram
                        current_time_secs = time.time()
                        if current_time_secs - last_update_time > 5:
                            last_update_time = current_time_secs
                            try:
                                progress_text = (
                                    f"🔄 Comprimiendo video: {progress_percent}%\n"
                                    f"⏱️ Tiempo procesado: {int(current_time // 60)}:{int(current_time % 60):02d} / "
                                    f"{int(total_duration // 60)}:{int(total_duration % 60):02d}\n"
                                    f"⏳ Por favor, espera..."
                                )
                                
                                if progress_message:
                                    await progress_message.edit_text(progress_text)
                                else:
                                    progress_message = await context.bot.send_message(
                                        chat_id=update.effective_chat.id,
                                        text=progress_text
                                    )
                            except Exception as e:
                                logger.error(f"Error al actualizar mensaje de progreso: {e}")
            
            # Verificar si la tarea fue cancelada
            if active_tasks[task_id]["status"] == "cancelled":
                process.kill()
                if progress_message:
                    await progress_message.edit_text("❌ Compresión cancelada.")
                return None
            
            await asyncio.sleep(0.1)
        
        # Esperar a que termine el proceso
        await process.wait()
        
        # Actualizar mensaje de progreso final
        if progress_message:
            await progress_message.edit_text("✅ Compresión completada. Preparando archivo...")
        
        return output_file

    except Exception as e:
        logger.error(f"Error durante la compresión: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error durante la compresión: {str(e)}"
        )
        return None

# Función para dividir un video en partes
async def split_video(input_file: str, output_dir: str, segment_time: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Crear directorio de salida si no existe
        os.makedirs(output_dir, exist_ok=True)
        
        # Obtener nombre base del archivo
        base_name = os.path.basename(input_file)
        name_without_ext = os.path.splitext(base_name)[0]
        
        # Configurar el patrón de salida
        output_pattern = os.path.join(output_dir, f"{name_without_ext}_part_%03d.mp4")
        
        # Mensaje de inicio
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🔄 Dividiendo video en segmentos de {segment_time} segundos...\n⏳ Esto puede tomar tiempo."
        )
        
        # Ejecutar FFmpeg para dividir el video
        process = (
            ffmpeg
            .input(input_file)
            .output(output_pattern, c='copy', map='0', f='segment', segment_time=segment_time)
            .global_args('-progress', 'pipe:1')
            .run_async(pipe_stdout=True, pipe_stderr=True)
        )
        
        # Esperar a que termine el proceso
        await process.wait()
        
        # Obtener lista de archivos generados
        output_files = [os.path.join(output_dir, f) for f in os.listdir(output_dir) if f.startswith(f"{name_without_ext}_part_")]
        output_files.sort()
        
        return output_files

    except Exception as e:
        logger.error(f"Error al dividir el video: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error al dividir el video: {str(e)}"
        )
        return []

# Función para extraer audio de un video
async def extract_audio(input_file: str, output_file: str, audio_format: str, audio_quality: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Configurar opciones según el formato
        audio_options = {}
        
        if audio_format == "mp3":
            audio_options = {
                'c:a': 'libmp3lame',
                'q:a': audio_quality
            }
        elif audio_format == "aac":
            audio_options = {
                'c:a': 'aac',
                'b:a': f"{audio_quality}k"
            }
        elif audio_format == "flac":
            audio_options = {
                'c:a': 'flac'
            }
        elif audio_format == "wav":
            audio_options = {
                'c:a': 'pcm_s16le'
            }
        
        # Mensaje de inicio
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🔄 Extrayendo audio en formato {audio_format.upper()}...\n⏳ Por favor, espera."
        )
        
        # Ejecutar FFmpeg para extraer audio
        process = (
            ffmpeg
            .input(input_file)
            .output(output_file, **audio_options)
            .global_args('-progress', 'pipe:1')
            .run_async(pipe_stdout=True, pipe_stderr=True)
        )
        
        # Esperar a que termine el proceso
        await process.wait()
        
        return output_file

    except Exception as e:
        logger.error(f"Error al extraer audio: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error al extraer audio: {str(e)}"
        )
        return None

# Función para recortar un video
async def trim_video(input_file: str, output_file: str, start_time: str, end_time: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Mensaje de inicio
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🔄 Recortando video desde {start_time} hasta {end_time}...\n⏳ Por favor, espera."
        )
        
        # Ejecutar FFmpeg para recortar el video
        process = (
            ffmpeg
            .input(input_file, ss=start_time, to=end_time)
            .output(output_file, c='copy')
            .global_args('-progress', 'pipe:1')
            .run_async(pipe_stdout=True, pipe_stderr=True)
        )
        
        # Esperar a que termine el proceso
        await process.wait()
        
        return output_file

    except Exception as e:
        logger.error(f"Error al recortar el video: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error al recortar el video: {str(e)}"
        )
        return None

# Función para extraer un fotograma del video
async def extract_frame(input_file: str, output_file: str, time_position: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Mensaje de inicio
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🔄 Extrayendo fotograma en la posición {time_position}...\n⏳ Por favor, espera."
        )
        
        # Ejecutar FFmpeg para extraer el fotograma
        process = (
            ffmpeg
            .input(input_file, ss=time_position)
            .output(output_file, vframes=1)
            .run_async(pipe_stdout=True, pipe_stderr=True)
        )
        
        # Esperar a que termine el proceso
        await process.wait()
        
        return output_file

    except Exception as e:
        logger.error(f"Error al extraer fotograma: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error al extraer fotograma: {str(e)}"
        )
        return None

# Función para añadir subtítulos a un video
async def add_subtitles(input_file: str, subtitle_file: str, output_file: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Mensaje de inicio
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🔄 Añadiendo subtítulos al video...\n⏳ Por favor, espera."
        )
        
        # Ejecutar FFmpeg para añadir subtítulos
        process = (
            ffmpeg
            .input(input_file)
            .output(output_file, vf=f"subtitles='{subtitle_file}'", c='copy')
            .global_args('-progress', 'pipe:1')
            .run_async(pipe_stdout=True, pipe_stderr=True)
        )
        
        # Esperar a que termine el proceso
        await process.wait()
        
        return output_file

    except Exception as e:
        logger.error(f"Error al añadir subtítulos: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error al añadir subtítulos: {str(e)}"
        )
        return None

# Comando /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    # Crear teclado con opciones principales
    keyboard = [
        [
            InlineKeyboardButton("🎬 Comprimir Video", callback_data="menu_compress"),
            InlineKeyboardButton("✂️ Dividir Video", callback_data="menu_split")
        ],
        [
            InlineKeyboardButton("🔊 Extraer Audio", callback_data="menu_extract_audio"),
            InlineKeyboardButton("✂️ Recortar Video", callback_data="menu_trim")
        ],
        [
            InlineKeyboardButton("🖼️ Extraer Fotograma", callback_data="menu_frame"),
            InlineKeyboardButton("⚙️ Configuración", callback_data="menu_settings")
        ],
        [
            InlineKeyboardButton("ℹ️ Información", callback_data="menu_info"),
            InlineKeyboardButton("📊 Estadísticas", callback_data="menu_stats")
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_html(
        f"¡Hola, {user.mention_html()}! 👋\n\n"
        f"Soy un bot avanzado para procesar videos. Puedo comprimir, dividir, extraer audio y más.\n\n"
        f"<b>Características principales:</b>\n"
        f"• Compresión de videos manteniendo calidad\n"
        f"• División de videos grandes en partes\n"
        f"• Extracción de audio en varios formatos\n"
        f"• Recorte de videos (trim)\n"
        f"• Extracción de fotogramas\n"
        f"• Ajuste de velocidad de reproducción\n"
        f"• Conversión entre formatos\n\n"
        f"<b>Limitaciones:</b>\n"
        f"• Tamaño máximo de archivo: {MAX_FILE_SIZE/(1024*1024):.1f}MB\n\n"
        f"Selecciona una opción o envíame directamente un video para comprimirlo:",
        reply_markup=reply_markup
    )

# Comando /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔍 *Ayuda del Bot de Procesamiento de Videos* 🔍\n\n"
        "*Comandos disponibles:*\n"
        "/start - Iniciar el bot y mostrar menú principal\n"
        "/help - Mostrar este mensaje de ayuda\n"
        "/settings - Configurar preferencias de compresión\n"
        "/status - Ver estado de tus tareas activas\n"
        "/cancel - Cancelar una tarea en curso\n"
        "/split - Dividir un video en partes más pequeñas\n"
        "/extract - Extraer audio de un video\n"
        "/trim - Recortar un video (especificar inicio y fin)\n"
        "/frame - Extraer un fotograma de un video\n"
        "/info - Ver información detallada de un video\n"
        "/stats - Ver tus estadísticas de uso\n"
        "/profiles - Guardar y cargar perfiles de configuración\n"
        "/about - Información sobre el bot\n\n"
        "*¿Cómo usar el bot?*\n"
        f"1. Envía un video o archivo de video (hasta {MAX_FILE_SIZE/(1024*1024):.1f}MB)\n"
        "2. Selecciona la acción que deseas realizar\n"
        "3. Configura las opciones si es necesario\n"
        "4. Espera mientras el bot procesa tu archivo\n\n"
        "*Formatos soportados:*\n"
        "MP4, MKV, AVI, MOV, WMV, FLV, WebM, etc.\n\n"
        "*Consejos:*\n"
        "- Usa /settings para personalizar la compresión\n"
        "- Para videos muy grandes, usa /split para dividirlos\n"
        "- Usa /extract para obtener solo el audio\n"
        "- Puedes cancelar cualquier proceso con /cancel\n"
        "- Guarda tus configuraciones favoritas con /profiles\n\n"
        "*Nota:* El bot mantiene la mejor calidad posible mientras reduce el tamaño.",
        parse_mode=ParseMode.MARKDOWN
    )

# Comando /settings
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    prefs = get_user_preferences(user_id)

    # Crear teclado inline con opciones
    keyboard = [
        [
            InlineKeyboardButton(f"Preset: {prefs['preset']}", callback_data="preset"),
            InlineKeyboardButton(f"CRF: {prefs['crf']}", callback_data="crf")
        ],
        [
            InlineKeyboardButton(f"Resolución: {prefs['resolution']}", callback_data="resolution"),
            InlineKeyboardButton(f"Audio: {prefs['audio_bitrate']}", callback_data="audio")
        ],
        [
            InlineKeyboardButton(f"Códec: {prefs['codec']}", callback_data="codec"),
            InlineKeyboardButton(f"Formato: {prefs['format']}", callback_data="format")
        ],
        [
            InlineKeyboardButton(f"Velocidad: {prefs['speed']}x", callback_data="speed")
        ],
        [
            InlineKeyboardButton("💾 Guardar como perfil", callback_data="save_profile")
        ],
        [
            InlineKeyboardButton("📂 Cargar perfil", callback_data="load_profile")
        ],
        [
            InlineKeyboardButton("🔄 Restablecer valores", callback_data="reset")
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    settings_text = (
        "⚙️ *Configuración de Compresión* ⚙️\n\n"
        "Personaliza cómo quieres procesar tus videos:\n\n"
        f"🎛 *Preset:* {prefs['preset']} (velocidad vs. compresión)\n"
        f"📊 *CRF:* {prefs['crf']} (calidad: menor = mejor)\n"
        f"📐 *Resolución:* {prefs['resolution']}\n"
        f"🔊 *Bitrate de audio:* {prefs['audio_bitrate']}\n"
        f"🎬 *Códec:* {prefs['codec']}\n"
        f"📦 *Formato de salida:* {prefs['format']}\n"
        f"⏩ *Velocidad:* {prefs['speed']}x\n\n"
        "Selecciona una opción para cambiarla:"
    )

    # Si es un comando directo, usar reply_text
    if update.message:
        await update.message.reply_text(
            settings_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    # Si es un callback, editar el mensaje existente
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            settings_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

# Comando /status
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_tasks = {task_id: task for task_id, task in active_tasks.items() if task["user_id"] == user_id}

    if not user_tasks:
        await update.message.reply_text("No tienes tareas activas en este momento.")
        return

    status_text = "📋 *Estado de tus tareas:*\n\n"

    for task_id, task in user_tasks.items():
        status_emoji = {
            "downloading": "⬇️",
            "processing": "⚙️",
            "uploading": "⬆️",
            "cancelled": "❌",
            "completed": "✅",
            "failed": "❌"
        }.get(task["status"], "❓")
        
        # Calcular tiempo transcurrido
        elapsed_time = datetime.now() - task["start_time"]
        elapsed_minutes = int(elapsed_time.total_seconds() // 60)
        elapsed_seconds = int(elapsed_time.total_seconds() % 60)
        
        status_text += (
            f"{status_emoji} *ID:* `{task_id[:8]}`\n"
            f"   *Archivo:* {task['filename']}\n"
            f"   *Estado:* {task['status']}\n"
            f"   *Tiempo:* {elapsed_minutes}m {elapsed_seconds}s\n\n"
        )

    # Añadir botón para cancelar tareas
    keyboard = [[InlineKeyboardButton("Cancelar una tarea", callback_data="cancel_task")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        status_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )

# Comando /cancel
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_tasks = {task_id: task for task_id, task in active_tasks.items() if task["user_id"] == user_id}

    if not user_tasks:
        await update.message.reply_text("No tienes tareas activas para cancelar.")
        return

    # Si solo hay una tarea, cancelarla directamente
    if len(user_tasks) == 1:
        task_id = list(user_tasks.keys())[0]
        active_tasks[task_id]["status"] = "cancelled"
        await update.message.reply_text(f"✅ Tarea cancelada: {user_tasks[task_id]['filename']}")
        return

    # Si hay múltiples tareas, mostrar opciones para cancelar
    keyboard = []
    for task_id, task in user_tasks.items():
        if task["status"] in ["downloading", "processing", "uploading"]:
            keyboard.append([
                InlineKeyboardButton(
                    f"{task['filename']} ({task['status']})",
                    callback_data=f"cancel_{task_id}"
                )
            ])

    if not keyboard:
        await update.message.reply_text("No tienes tareas activas para cancelar.")
        return

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Selecciona la tarea que deseas cancelar:",
        reply_markup=reply_markup
    )

# Comando /split
async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "✂️ *Dividir Video en Partes* ✂️\n\n"
        "Esta función te permite dividir un video grande en partes más pequeñas.\n\n"
        "Para usar esta función:\n"
        "1. Envía el video que deseas dividir\n"
        "2. Responde al video con /split\n"
        "3. Selecciona la duración de cada parte\n\n"
        "También puedes especificar la duración directamente:\n"
        "/split 10min - Divide en partes de 10 minutos\n"
        "/split 5min - Divide en partes de 5 minutos\n"
        "/split 2min - Divide en partes de 2 minutos\n\n"
        "*Nota:* Asegúrate de responder a un video con este comando.",
        parse_mode=ParseMode.MARKDOWN
    )

# Comando /extract
async def extract_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔊 *Extraer Audio de Video* 🔊\n\n"
        "Esta función te permite extraer el audio de un video en diferentes formatos.\n\n"
        "Para usar esta función:\n"
        "1. Envía el video del que deseas extraer el audio\n"
        "2. Responde al video con /extract\n"
        "3. Selecciona el formato de audio deseado\n\n"
        "Formatos disponibles:\n"
        "- MP3 (varios niveles de calidad)\n"
        "- AAC (varios bitrates)\n"
        "- FLAC (sin pérdida)\n"
        "- WAV (sin compresión)\n\n"
        "*Nota:* Asegúrate de responder a un video con este comando.",
        parse_mode=ParseMode.MARKDOWN
    )

# Comando /trim
async def trim_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "✂️ *Recortar Video* ✂️\n\n"
        "Esta función te permite recortar un segmento específico de un video.\n\n"
        "Para usar esta función:\n"
        "1. Envía el video que deseas recortar\n"
        "2. Responde al video con /trim\n"
        "3. Especifica el tiempo de inicio (formato: HH:MM:SS)\n"
        "4. Especifica el tiempo de fin (formato: HH:MM:SS)\n\n"
        "También puedes especificar los tiempos directamente:\n"
        "/trim 00:01:30 00:02:45 - Recorta desde 1:30 hasta 2:45\n\n"
        "*Nota:* Asegúrate de responder a un video con este comando.",
        parse_mode=ParseMode.MARKDOWN
    )

# Comando /frame
async def frame_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🖼️ *Extraer Fotograma de Video* 🖼️\n\n"
        "Esta función te permite extraer un fotograma específico de un video.\n\n"
        "Para usar esta función:\n"
        "1. Envía el video del que deseas extraer un fotograma\n"
        "2. Responde al video con /frame\n"
        "3. Especifica el tiempo del fotograma (formato: HH:MM:SS)\n\n"
        "También puedes especificar el tiempo directamente:\n"
        "/frame 00:01:30 - Extrae el fotograma en el minuto 1:30\n\n"
        "*Nota:* Asegúrate de responder a un video con este comando.",
        parse_mode=ParseMode.MARKDOWN
    )

# Comando /info
async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Verificar si es una respuesta a un video
    if update.message and update.message.reply_to_message and (update.message.reply_to_message.video or update.message.reply_to_message.document):
        # Obtener el archivo
        if update.message.reply_to_message.video:
            file = update.message.reply_to_message.video
        else:
            file = update.message.reply_to_message.document
        
        # Verificar el tamaño del archivo
        if file.file_size > MAX_FILE_SIZE:
            await update.message.reply_text(
                f"⚠️ Lo siento, el archivo es demasiado grande. El tamaño máximo permitido es {MAX_FILE_SIZE/(1024*1024):.1f}MB."
            )
            return
        
        # Descargar el archivo
        status_message = await update.message.reply_text("⏳ Descargando archivo para analizar...")
        
        try:
            file_obj = await context.bot.get_file(file.file_id)
            temp_file = os.path.join(TEMP_DOWNLOAD_DIR, f"info_{int(time.time())}_{file.file_name if hasattr(file, 'file_name') else 'video.mp4'}")
            await file_obj.download_to_drive(custom_path=temp_file)
            
            # Actualizar mensaje
            await status_message.edit_text("🔍 Analizando video... Por favor, espera.")
            
            # Obtener información del video
            video_info = await get_video_info(temp_file)
            
            if video_info:
                # Formatear información
                duration_min = int(video_info['duration'] // 60)
                duration_sec = int(video_info['duration'] % 60)
                
                info_text = (
                    "📋 *Información del Video* 📋\n\n"
                    f"*Nombre:* {file.file_name if hasattr(file, 'file_name') else 'Video'}\n"
                    f"*Formato:* {video_info['format']}\n"
                    f"*Duración:* {duration_min}:{duration_sec:02d}\n"
                    f"*Tamaño:* {video_info['size'] / (1024*1024):.2f} MB\n"
                    f"*Bitrate total:* {video_info['bit_rate'] / 1000:.0f} kbps\n\n"
                )
                
                if video_info.get('video'):
                    v_info = video_info['video']
                    info_text += (
                        "*Pista de Video:*\n"
                        f"- Códec: {v_info.get('codec', 'N/A')}\n"
                        f"- Resolución: {v_info.get('width', 0)}x{v_info.get('height', 0)}\n"
                        f"- FPS: {v_info.get('fps', 0):.2f}\n"
                        f"- Bitrate: {v_info.get('bit_rate', 0) / 1000:.0f} kbps\n\n"
                    )
                
                if video_info.get('audio'):
                    a_info = video_info['audio']
                    info_text += (
                        "*Pista de Audio:*\n"
                        f"- Códec: {a_info.get('codec', 'N/A')}\n"
                        f"- Canales: {a_info.get('channels', 0)}\n"
                        f"- Frecuencia: {a_info.get('sample_rate', 0) / 1000:.1f} kHz\n"
                        f"- Bitrate: {a_info.get('bit_rate', 0) / 1000:.0f} kbps\n\n"
                    )
                
                if video_info.get('subtitles'):
                    info_text += "*Subtítulos:*\n"
                    for i, sub in enumerate(video_info['subtitles']):
                        info_text += f"- Pista {i+1}: {sub.get('codec', 'N/A')} ({sub.get('language', 'N/A')})\n"
                
                # Extraer un fotograma para mostrar como thumbnail
                thumbnail_path = os.path.join(TEMP_EXTRACT_DIR, f"thumb_{int(time.time())}.jpg")
                await extract_frame(temp_file, thumbnail_path, "00:00:05", update, context)
                
                # Enviar información con thumbnail
                if os.path.exists(thumbnail_path):
                    with open(thumbnail_path, 'rb') as thumb:
                        await update.message.reply_photo(
                            photo=thumb,
                            caption=info_text,
                            parse_mode=ParseMode.MARKDOWN
                        )
                    # Limpiar thumbnail
                    os.remove(thumbnail_path)
                else:
                    await update.message.reply_text(
                        info_text,
                        parse_mode=ParseMode.MARKDOWN
                    )
                
                # Limpiar mensaje de estado
                await status_message.delete()
            else:
                await status_message.edit_text("❌ No se pudo obtener información del video.")
            
            # Limpiar archivo temporal
            if os.path.exists(temp_file):
                os.remove(temp_file)
        
        except Exception as e:
            logger.error(f"Error al obtener información del video: {e}")
            await status_message.edit_text(f"❌ Error al analizar el video: {str(e)}")

    else:
        await update.message.reply_text(
            "ℹ️ Para usar este comando, responde a un video con /info\n\n"
            "Este comando te mostrará información detallada sobre el video, incluyendo:\n"
            "- Formato y duración\n"
            "- Resolución y FPS\n"
            "- Códecs de video y audio\n"
            "- Bitrates\n"
            "- Información de subtítulos\n"
            "- Y más..."
        )

# Comando /stats
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)

    if user_id not in user_stats:
        await update.message.reply_text("No tienes estadísticas de uso todavía.")
        return

    stats = user_stats[user_id]

    # Calcular estadísticas
    total_files = stats.get("total_files", 0)
    total_original_mb = stats.get("total_original_size", 0) / (1024 * 1024)
    total_compressed_mb = stats.get("total_compressed_size", 0) / (1024 * 1024)
    space_saved_mb = stats.get("space_saved", 0) / (1024 * 1024)

    if total_original_mb > 0:
        reduction_percent = (space_saved_mb / total_original_mb) * 100
    else:
        reduction_percent = 0

    # Formatear última actividad
    last_activity = "Desconocida"
    if "last_activity" in stats:
        try:
            last_activity_dt = datetime.fromisoformat(stats["last_activity"])
            last_activity = last_activity_dt.strftime("%d/%m/%Y %H:%M")
        except:
            pass

    stats_text = (
        "📊 *Tus Estadísticas de Uso* 📊\n\n"
        f"*Archivos procesados:* {total_files}\n"
        f"*Tamaño original total:* {total_original_mb:.2f} MB\n"
        f"*Tamaño comprimido total:* {total_compressed_mb:.2f} MB\n"
        f"*Espacio ahorrado:* {space_saved_mb:.2f} MB ({reduction_percent:.1f}%)\n"
        f"*Última actividad:* {last_activity}\n\n"
        "Sigue usando el bot para procesar más videos y ahorrar espacio."
    )

    await update.message.reply_text(
        stats_text,
        parse_mode=ParseMode.MARKDOWN
    )

# Comando /profiles
async def profiles_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)

    # Obtener perfiles guardados
    profiles_dir = os.path.join(PROFILES_DIR, user_id)
    os.makedirs(profiles_dir, exist_ok=True)

    profiles = [f[:-5] for f in os.listdir(profiles_dir) if f.endswith('.json')]

    if not profiles:
        keyboard = [
            [InlineKeyboardButton("💾 Guardar configuración actual", callback_data="save_new_profile")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "📁 *Perfiles de Configuración* 📁\n\n"
            "No tienes perfiles guardados.\n\n"
            "Puedes guardar tu configuración actual como un perfil para usarla más tarde.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    else:
        keyboard = []
        
        # Añadir botones para cargar perfiles
        for profile in profiles:
            keyboard.append([
                InlineKeyboardButton(f"📂 Cargar: {profile}", callback_data=f"load_profile_{profile}"),
                InlineKeyboardButton("🗑️", callback_data=f"delete_profile_{profile}")
            ])
        
        # Añadir botón para guardar nuevo perfil
        keyboard.append([
            InlineKeyboardButton("💾 Guardar configuración actual", callback_data="save_new_profile")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "📁 *Perfiles de Configuración* 📁\n\n"
            "Selecciona un perfil para cargarlo o guarda tu configuración actual como un nuevo perfil.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )

# Comando /about
async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 *Bot Avanzado de Procesamiento de Videos* 🤖\n\n"
        "Este bot te ayuda a procesar videos de múltiples formas, manteniendo la mejor calidad posible.\n\n"
        "*Características:*\n"
        "• Compresión eficiente de videos\n"
        "• División de videos en partes\n"
        "• Extracción de audio en varios formatos\n"
        "• Recorte de videos (trim)\n"
        "• Extracción de fotogramas\n"
        "• Ajuste de velocidad de reproducción\n"
        "• Conversión entre formatos\n"
        "• Perfiles de configuración personalizados\n"
        "• Estadísticas de uso y ahorro\n\n"
        "*Tecnología:*\n"
        "• FFmpeg para procesamiento de video\n"
        "• Códecs H.264/H.265/VP9 para compresión eficiente\n\n"
        "*Limitaciones:*\n"
        f"• Tamaño máximo de archivo: {MAX_FILE_SIZE/(1024*1024):.1f}MB\n\n"
        "*Versión:* 2.0.0\n\n"
        "¡Gracias por usar este bot!",
        parse_mode=ParseMode.MARKDOWN
    )

# Manejador para videos y archivos
@send_action(ChatAction.TYPING)
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    # Determinar si es un video o un archivo
    if update.message.video:
        file = update.message.video
        file_name = file.file_name if hasattr(file, 'file_name') and file.file_name else f"video_{int(time.time())}.mp4"
    elif update.message.document:
        file = update.message.document
        file_name = file.file_name if hasattr(file, 'file_name') and file.file_name else f"file_{int(time.time())}"
    else:
        await update.message.reply_text("Por favor, envía un video o un archivo de video.")
        return
    
    # Verificar el tamaño del archivo
    if file.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            f"⚠️ Lo siento, el archivo es demasiado grande. El tamaño máximo permitido es {MAX_FILE_SIZE/(1024*1024):.1f}MB.\n\n"
            f"Puedes:\n"
            f"1. Usar el comando /split para dividir el archivo en partes más pequeñas\n"
            f"2. Enviar un archivo más pequeño\n"
            f"3. Usar el comando /extract para extraer solo el audio"
        )
        return
    
    # Guardar el mensaje original para referencia futura
    message_id = update.message.message_id
    original_messages[str(message_id)] = {
        "file_id": file.file_id,
        "file_name": file_name,
        "file_size": file.file_size,
        "type": "video" if update.message.video else "document"
    }
    
    # Mostrar opciones de procesamiento con referencia al mensaje original
    keyboard = [
        [
            InlineKeyboardButton("🎬 Comprimir", callback_data=f"comp_{message_id}"),
            InlineKeyboardButton("✂️ Dividir", callback_data=f"split_{message_id}")
        ],
        [
            InlineKeyboardButton("🔊 Extraer Audio", callback_data=f"audio_{message_id}"),
            InlineKeyboardButton("✂️ Recortar", callback_data=f"trim_{message_id}")
        ],
        [
            InlineKeyboardButton("🖼️ Extraer Fotograma", callback_data=f"frame_{message_id}"),
            InlineKeyboardButton("ℹ️ Información", callback_data=f"info_{message_id}")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await update.message.reply_text(
            f"📁 Archivo recibido: {file_name}\n"
            f"📊 Tamaño: {file.file_size / (1024 * 1024):.2f} MB\n\n"
            f"Selecciona qué acción deseas realizar con este archivo:",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error al enviar mensaje con botones: {e}")
        # Enviar mensaje sin botones como fallback
        await update.message.reply_text(
            f"📁 Archivo recibido: {file_name}\n"
            f"📊 Tamaño: {file.file_size / (1024 * 1024):.2f} MB\n\n"
            f"Usa los comandos /compress, /split, /extract, /trim, /frame o /info respondiendo a este mensaje."
        )

# Manejador para callbacks de botones inline
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    callback_data = query.data
    
    # Manejar menú principal
    if callback_data.startswith("menu_"):
        action = callback_data[5:]
        
        if action == "compress":
            await settings_command(update, context)
        
        elif action == "split":
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="✂️ *Dividir Video en Partes* ✂️\n\n"
                    "Esta función te permite dividir un video grande en partes más pequeñas.\n\n"
                    "Para usar esta función:\n"
                    "1. Envía el video que deseas dividir\n"
                    "2. Selecciona la opción 'Dividir' en el menú\n"
                    "3. Elige la duración de cada parte\n\n"
                    "*Nota:* También puedes usar el comando /split respondiendo a un video.",
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif action == "extract_audio":
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="🔊 *Extraer Audio de Video* 🔊\n\n"
                    "Esta función te permite extraer el audio de un video en diferentes formatos.\n\n"
                    "Para usar esta función:\n"
                    "1. Envía el video del que deseas extraer el audio\n"
                    "2. Selecciona la opción 'Extraer Audio' en el menú\n"
                    "3. Elige el formato y calidad del audio\n\n"
                    "*Nota:* También puedes usar el comando /extract respondiendo a un video.",
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Resto del código para el menú principal...
    
    # Obtener el mensaje original al que se refiere el callback
    elif any(callback_data.startswith(prefix) for prefix in ["comp_", "split_", "audio_", "trim_", "frame_", "info_"]):
        # Extraer el ID del mensaje original
        parts = callback_data.split("_")
        prefix = parts[0]
        try:
            message_id = parts[1]
            
            # Obtener la información del mensaje original
            if message_id in original_messages:
                file_info = original_messages[message_id]
                
                # Procesar según la acción solicitada
                if prefix == "comp":
                    await process_compression_from_info(update, context, file_info)
                elif prefix == "split":
                    await process_split_from_info(update, context, file_info)
                elif prefix == "audio":
                    await process_extract_audio_from_info(update, context, file_info)
                elif prefix == "trim":
                    await process_trim_from_info(update, context, file_info)
                elif prefix == "frame":
                    await process_frame_from_info(update, context, file_info)
                elif prefix == "info":
                    await process_info_from_info(update, context, file_info)
                
            else:
                logger.error(f"No se encontró información para el mensaje ID: {message_id}")
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="❌ No se pudo procesar el video. Por favor, envía el video nuevamente."
                )
        
        except Exception as e:
            logger.error(f"Error al procesar callback: {e}")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Error al procesar la solicitud. Por favor, intenta nuevamente."
            )
    
    # Resto del código para manejar otros callbacks...

# Nuevas funciones para procesar acciones desde información almacenada

async def process_compression_from_info(update: Update, context: ContextTypes.DEFAULT_TYPE, file_info: dict) -> None:
    """Procesa la compresión de un video desde información almacenada."""
    user_id = update.effective_user.id
    file_id = file_info["file_id"]
    file_name = file_info["file_name"]
    file_size = file_info["file_size"]
    
    # Crear ID único para esta tarea
    task_id = str(uuid.uuid4())
    
    # Crear rutas para archivos temporales
    download_path = os.path.join(TEMP_DOWNLOAD_DIR, f"{task_id}_{file_name}")
    output_path = os.path.join(TEMP_COMPRESSED_DIR, f"compressed_{task_id}_{file_name}")
    
    # Registrar la tarea
    active_tasks[task_id] = {
    "user_id": user_id,
    "filename": file_name,
    "status": "downloading",
    "start_time": datetime.now(),
    "original_size": file_size,
    "compressed_size": None,
    "download_path": download_path,
    "output_path": output_path
}
    
    # Informar al usuario
    status_message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"📥 Recibiendo archivo: {file_name}\n"
            f"📊 Tamaño original: {file_size / (1024 * 1024):.2f} MB\n\n"
            f"⏳ Descargando... Por favor, espera."
    )
    
    try:
        # Descargar el archivo
        file_obj = await context.bot.get_file(file_id)
        await file_obj.download_to_drive(custom_path=download_path)
        
        # Actualizar estado
        active_tasks[task_id]["status"] = "processing"
        await status_message.edit_text(
            f"✅ Archivo recibido: {file_name}\n"
            f"📊 Tamaño original: {file_size / (1024 * 1024):.2f} MB\n\n"
            f"🔄 Iniciando compresión... Esto puede tomar tiempo."
        )
        
        # Obtener preferencias del usuario
        preferences = get_user_preferences(user_id)
        
        # Comprimir el video
        compressed_file = await compress_video(
            download_path, 
            output_path, 
            preferences, 
            update, 
            context, 
            task_id
        )
        
        # Verificar si la compresión fue cancelada
        if active_tasks[task_id]["status"] == "cancelled":
            await status_message.edit_text("❌ Compresión cancelada por el usuario.")
            # Limpiar archivos
            if os.path.exists(download_path):
                os.remove(download_path)
            if os.path.exists(output_path):
                os.remove(output_path)
            return
        
        # Verificar si la compresión fue exitosa
        if not compressed_file or not os.path.exists(compressed_file):
            active_tasks[task_id]["status"] = "failed"
            await status_message.edit_text("❌ Error durante la compresión. Por favor, intenta nuevamente.")
            return
        
        # Obtener tamaño del archivo comprimido
        compressed_size = os.path.getsize(compressed_file)
        active_tasks[task_id]["compressed_size"] = compressed_size
        
        # Calcular estadísticas
        original_mb = file_size / (1024 * 1024)
        compressed_mb = compressed_size / (1024 * 1024)
        reduction_percent = ((file_size - compressed_size) / file_size) * 100
        
        # Actualizar estado
        active_tasks[task_id]["status"] = "uploading"
        await status_message.edit_text(
            f"✅ Compresión completada\n"
            f"📊 Tamaño original: {original_mb:.2f} MB\n"
            f"📊 Tamaño comprimido: {compressed_mb:.2f} MB\n"
            f"📉 Reducción: {reduction_percent:.1f}%\n\n"
            f"⏳ Subiendo archivo comprimido..."
        )
        
        # Enviar el archivo comprimido
        try:
            with open(compressed_file, 'rb') as f:
                if compressed_file.lower().endswith(('.mp4', '.avi', '.mov', '.mkv')):
                    await context.bot.send_video(
                        chat_id=update.effective_chat.id,
                        video=f,
                        caption=f"🎬 Video comprimido\n"
                                f"📊 Tamaño original: {original_mb:.2f} MB\n"
                                f"📊 Tamaño comprimido: {compressed_mb:.2f} MB\n"
                                f"📉 Reducción: {reduction_percent:.1f}%",
                        supports_streaming=True
                    )
                else:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=f,
                        caption=f"🎬 Video comprimido\n"
                                f"📊 Tamaño original: {original_mb:.2f} MB\n"
                                f"📊 Tamaño comprimido: {compressed_mb:.2f} MB\n"
                                f"📉 Reducción: {reduction_percent:.1f}%"
                    )
            
            # Actualizar estado final
            active_tasks[task_id]["status"] = "completed"
            await status_message.edit_text(
                f"✅ Proceso completado\n"
                f"📊 Tamaño original: {original_mb:.2f} MB\n"
                f"📊 Tamaño comprimido: {compressed_mb:.2f} MB\n"
                f"📉 Reducción: {reduction_percent:.1f}%\n\n"
                f"El archivo comprimido ha sido enviado."
            )
            
            # Actualizar estadísticas de usuario
            update_user_stats(user_id, file_size, compressed_size)
            
        except TelegramError as e:
            # Si el archivo comprimido es demasiado grande para Telegram
            if "File is too big" in str(e):
                active_tasks[task_id]["status"] = "failed"
                await status_message.edit_text(
                    f"❌ El archivo comprimido sigue siendo demasiado grande para Telegram.\n"
                    f"📊 Tamaño comprimido: {compressed_mb:.2f} MB\n\n"
                    f"Sugerencias:\n"
                    f"1. Intenta con una resolución más baja (480p o 360p)\n"
                    f"2. Usa un CRF más alto (26-28)\n"
                    f"3. Usa el comando /split para dividir el video en partes más pequeñas"
                )
            else:
                active_tasks[task_id]["status"] = "failed"
                await status_message.edit_text(f"❌ Error al enviar el archivo: {str(e)}")
    
    except Exception as e:
        logger.error(f"Error en el proceso de compresión: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error: {str(e)}"
        )
    
    finally:
        # Limpiar archivos temporales
        try:
            if os.path.exists(download_path):
                os.remove(download_path)
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception as e:
            logger.error(f"Error al limpiar archivos temporales: {e}")

async def process_split_from_info(update: Update, context: ContextTypes.DEFAULT_TYPE, file_info: dict) -> None:
    """Procesa la división de un video desde información almacenada."""
    # Mostrar opciones de duración para dividir
    message_id = file_info["message_id"] if "message_id" in file_info else str(int(time.time()))
    
    keyboard = [
        [
            InlineKeyboardButton("2 minutos", callback_data=f"split_duration_{message_id}_120"),
            InlineKeyboardButton("5 minutos", callback_data=f"split_duration_{message_id}_300")
        ],
        [
            InlineKeyboardButton("10 minutos", callback_data=f"split_duration_{message_id}_600"),
            InlineKeyboardButton("15 minutos", callback_data=f"split_duration_{message_id}_900")
        ],
        [
            InlineKeyboardButton("20 minutos", callback_data=f"split_duration_{message_id}_1200"),
            InlineKeyboardButton("30 minutos", callback_data=f"split_duration_{message_id}_1800")
        ],
        [
            InlineKeyboardButton("Personalizado", callback_data=f"split_custom_{message_id}")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✂️ *Dividir Video en Partes* ✂️\n\n"
            "Selecciona la duración de cada parte:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def process_extract_audio_from_info(update: Update, context: ContextTypes.DEFAULT_TYPE, file_info: dict) -> None:
    """Procesa la extracción de audio desde información almacenada."""
    # Mostrar opciones de formato de audio
    message_id = file_info["message_id"] if "message_id" in file_info else str(int(time.time()))
    
    keyboard = [
        [
            InlineKeyboardButton("MP3 (Alta Calidad)", callback_data=f"extract_audio_format_{message_id}_mp3_0"),
            InlineKeyboardButton("MP3 (Media Calidad)", callback_data=f"extract_audio_format_{message_id}_mp3_5")
        ],
        [
            InlineKeyboardButton("AAC (256k)", callback_data=f"extract_audio_format_{message_id}_aac_256"),
            InlineKeyboardButton("AAC (128k)", callback_data=f"extract_audio_format_{message_id}_aac_128")
        ],
        [
            InlineKeyboardButton("FLAC (Sin pérdida)", callback_data=f"extract_audio_format_{message_id}_flac_0"),
            InlineKeyboardButton("WAV (Sin compresión)", callback_data=f"extract_audio_format_{message_id}_wav_0")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔊 *Extraer Audio de Video* 🔊\n\n"
            "Selecciona el formato y calidad del audio:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def process_trim_from_info(update: Update, context: ContextTypes.DEFAULT_TYPE, file_info: dict) -> None:
    """Procesa el recorte de un video desde información almacenada."""
    # Guardar la información del archivo en el contexto del usuario
    context.user_data["trim_file_info"] = file_info
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✂️ *Recortar Video* ✂️\n\n"
            "Por favor, especifica el tiempo de inicio (formato: HH:MM:SS):",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Establecer el estado de conversación
    context.user_data["waiting_for_trim_start"] = True

async def process_frame_from_info(update: Update, context: ContextTypes.DEFAULT_TYPE, file_info: dict) -> None:
    """Procesa la extracción de un fotograma desde información almacenada."""
    # Guardar la información del archivo en el contexto del usuario
    context.user_data["frame_file_info"] = file_info
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🖼️ *Extraer Fotograma* 🖼️\n\n"
            "Por favor, especifica el tiempo del fotograma (formato: HH:MM:SS):",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Establecer el estado de conversación
    context.user_data["waiting_for_frame_time"] = True

async def process_info_from_info(update: Update, context: ContextTypes.DEFAULT_TYPE, file_info: dict) -> None:
    """Procesa la obtención de información de un video desde información almacenada."""
    file_id = file_info["file_id"]
    file_name = file_info["file_name"]
    file_size = file_info["file_size"]
    
    # Verificar el tamaño del archivo
    if file_size > MAX_FILE_SIZE:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ Lo siento, el archivo es demasiado grande. El tamaño máximo permitido es {MAX_FILE_SIZE/(1024*1024):.1f}MB."
        )
        return
    
    # Descargar el archivo
    status_message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏳ Descargando archivo para analizar..."
    )
    
    try:
        file_obj = await context.bot.get_file(file_id)
        temp_file = os.path.join(TEMP_DOWNLOAD_DIR, f"info_{int(time.time())}_{file_name}")
        await file_obj.download_to_drive(custom_path=temp_file)
        
        # Actualizar mensaje
        await status_message.edit_text("🔍 Analizando video... Por favor, espera.")
        
        # Obtener información del video
        video_info = await get_video_info(temp_file)
        
        if video_info:
            # Formatear información
            duration_min = int(video_info['duration'] // 60)
            duration_sec = int(video_info['duration'] % 60)
            
            info_text = (
                "📋 *Información del Video* 📋\n\n"
                f"*Nombre:* {file_name}\n"
                f"*Formato:* {video_info['format']}\n"
                f"*Duración:* {duration_min}:{duration_sec:02d}\n"
                f"*Tamaño:* {video_info['size'] / (1024*1024):.2f} MB\n"
                f"*Bitrate total:* {video_info['bit_rate'] / 1000:.0f} kbps\n\n"
            )
            
            if video_info.get('video'):
                v_info = video_info['video']
                info_text += (
                    "*Pista de Video:*\n"
                    f"- Códec: {v_info.get('codec', 'N/A')}\n"
                    f"- Resolución: {v_info.get('width', 0)}x{v_info.get('height', 0)}\n"
                    f"- FPS: {v_info.get('fps', 0):.2f}\n"
                    f"- Bitrate: {v_info.get('bit_rate', 0) / 1000:.0f} kbps\n\n"
                )
            
            if video_info.get('audio'):
                a_info = video_info['audio']
                info_text += (
                    "*Pista de Audio:*\n"
                    f"- Códec: {a_info.get('codec', 'N/A')}\n"
                    f"- Canales: {a_info.get('channels', 0)}\n"
                    f"- Frecuencia: {a_info.get('sample_rate', 0) / 1000:.1f} kHz\n"
                    f"- Bitrate: {a_info.get('bit_rate', 0) / 1000:.0f} kbps\n\n"
                )
            
            if video_info.get('subtitles'):
                info_text += "*Subtítulos:*\n"
                for i, sub in enumerate(video_info['subtitles']):
                    info_text += f"- Pista {i+1}: {sub.get('codec', 'N/A')} ({sub.get('language', 'N/A')})\n"
            
            # Extraer un fotograma para mostrar como thumbnail
            thumbnail_path = os.path.join(TEMP_EXTRACT_DIR, f"thumb_{int(time.time())}.jpg")
            await extract_frame(temp_file, thumbnail_path, "00:00:05", update, context)
            
            # Enviar información con thumbnail
            if os.path.exists(thumbnail_path):
                with open(thumbnail_path, 'rb') as thumb:
                    await context.bot.send_photo(
                        chat_id=update.effective_chat.id,
                        photo=thumb,
                        caption=info_text,
                        parse_mode=ParseMode.MARKDOWN
                    )
                # Limpiar thumbnail
                os.remove(thumbnail_path)
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=info_text,
                    parse_mode=ParseMode.MARKDOWN
                )
            
            # Limpiar mensaje de estado
            await status_message.delete()
        else:
            await status_message.edit_text("❌ No se pudo obtener información del video.")
        
        # Limpiar archivo temporal
        if os.path.exists(temp_file):
            os.remove(temp_file)
    
    except Exception as e:
        logger.error(f"Error al obtener información del video: {e}")
        await status_message.edit_text(f"❌ Error al analizar el video: {str(e)}")

# Función para limpiar tareas antiguas y archivos temporales
async def cleanup_tasks(context: ContextTypes.DEFAULT_TYPE) -> None:
    current_time = datetime.now()
    tasks_to_remove = []

    # Identificar tareas completadas hace más de 1 hora
    for task_id, task in active_tasks.items():
        if task["status"] in ["completed", "failed", "cancelled"]:
            time_diff = current_time - task["start_time"]
            if time_diff.total_seconds() > 3600:  # 1 hora
                tasks_to_remove.append(task_id)
                
                # Limpiar archivos asociados si existen
                for path_key in ["download_path", "output_path", "output_dir"]:
                    if path_key in task and task[path_key]:
                        path = task[path_key]
                        if os.path.exists(path):
                            try:
                                if os.path.isdir(path):
                                    shutil.rmtree(path)
                                else:
                                    os.remove(path)
                            except Exception as e:
                                logger.error(f"Error al eliminar archivo temporal {path}: {e}")

    # Eliminar tareas antiguas
    for task_id in tasks_to_remove:
        del active_tasks[task_id]

    # Limpiar archivos huérfanos en directorios temporales
    for temp_dir in [TEMP_DOWNLOAD_DIR, TEMP_COMPRESSED_DIR, TEMP_SPLIT_DIR, TEMP_EXTRACT_DIR]:
        try:
            if os.path.exists(temp_dir):
                for filename in os.listdir(temp_dir):
                    file_path = os.path.join(temp_dir, filename)
                    file_modified_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                    if (current_time - file_modified_time).total_seconds() > 7200:  # 2 horas
                        try:
                            if os.path.isdir(file_path):
                                shutil.rmtree(file_path)
                            else:
                                os.remove(file_path)
                        except Exception as e:
                            logger.error(f"Error al limpiar archivo temporal {file_path}: {e}")
        except Exception as e:
            logger.error(f"Error al limpiar directorio temporal {temp_dir}: {e}")

    # Limpiar mensajes originales antiguos (más de 24 horas)
    messages_to_remove = []
    for message_id, message_info in original_messages.items():
        if "timestamp" in message_info:
            message_time = datetime.fromtimestamp(message_info["timestamp"])
            if (current_time - message_time).total_seconds() > 86400:  # 24 horas
                messages_to_remove.append(message_id)
    
    for message_id in messages_to_remove:
        del original_messages[message_id]

# Manejador de errores global
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Maneja errores del bot."""
    logger.error(f"Error al procesar actualización {update}: {context.error}")

    # Obtener el chat_id para enviar mensaje de error
    if update and update.effective_chat:
        chat_id = update.effective_chat.id
        
        # Enviar mensaje de error al usuario
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Ha ocurrido un error inesperado. Por favor, intenta nuevamente más tarde."
        )

def main() -> None:
    """Iniciar el bot."""
    # Crear la aplicación
    application = Application.builder().token(TOKEN).job_queue(True).build()

    # Añadir manejadores de comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("split", split_command))
    application.add_handler(CommandHandler("extract", extract_command))
    application.add_handler(CommandHandler("trim", trim_command))
    application.add_handler(CommandHandler("frame", frame_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("profiles", profiles_command))
    application.add_handler(CommandHandler("about", about_command))

    # Añadir manejadores para videos y archivos
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))

    # Añadir manejador para callbacks de botones
    application.add_handler(CallbackQueryHandler(button_callback))

    # Añadir manejador de errores
    application.add_error_handler(error_handler)

    # Programar tarea de limpieza cada 30 minutos
    application.job_queue.run_repeating(cleanup_tasks, interval=1800, first=1800)

    # Ejecutar el bot hasta que se presione Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
