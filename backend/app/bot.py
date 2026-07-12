"""
Telegram Bot handlers using PyroTGFork MTProto.
Handles commands, file uploads, and inline callbacks.
"""

import asyncio
import secrets
import string
import re
from datetime import datetime, timedelta
from pyrogram import filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from sqlalchemy import select, func, update, text
from sqlalchemy.exc import IntegrityError

from .telegram import tg_client, forward_to_storage_channel
from .database import async_session
from .models import User, File, Folder, LoginCode, WebCredential, AuthSession
from .config import get_settings
from .time_utils import utcnow
from .auth import hash_password, normalize_password_secret
from .username_rules import RESERVED_USERNAMES, BANNED_USERNAME_PARTS
from .services import sanitize_filename, build_public_stream_path
from .media_types import resolve_media_type
from .recycle_bin import trash_files, trash_folder

settings = get_settings()
USERNAME_RE = re.compile(r"^[a-z](?:[a-z0-9._-]*[a-z0-9])?$")



_pending_bot_actions: set[int] = set()
_duplicate_upload_overrides: set[tuple[int, int]] = set()


def begin_pending_action(chat_id: int) -> bool:
    """Prevent overlapping wait_for_message flows in the same private chat."""
    if chat_id in _pending_bot_actions:
        return False
    _pending_bot_actions.add(chat_id)
    return True


def clear_pending_action(chat_id: int) -> None:
    _pending_bot_actions.discard(chat_id)


def clean_bot_folder_name(name: str | None) -> str | None:
    cleaned = sanitize_filename(name or "").strip()
    return cleaned or None


async def get_bot_user(db, telegram_id: int) -> User | None:
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def get_owned_file(db, user_id: int, file_id: int) -> File | None:
    result = await db.execute(
        select(File).where(File.id == file_id, File.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_owned_folder(db, user_id: int, folder_id: int) -> Folder | None:
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def bot_folder_name_exists(
    db,
    user_id: int,
    folder_name: str,
    parent_id: int | None,
    exclude_folder_id: int | None = None,
) -> bool:
    stmt = select(Folder.id).where(Folder.user_id == user_id, Folder.name == folder_name)
    if parent_id is None:
        stmt = stmt.where(Folder.parent_id.is_(None))
    else:
        stmt = stmt.where(Folder.parent_id == parent_id)
    if exclude_folder_id is not None:
        stmt = stmt.where(Folder.id != exclude_folder_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def get_descendant_folder_ids_for_user(db, folder_id: int, user_id: int) -> list[int]:
    query = text("""
        WITH RECURSIVE subfolders AS (
            SELECT id FROM folders WHERE id = :root_id AND user_id = :user_id AND deleted_at IS NULL
            UNION ALL
            SELECT f.id FROM folders f
            INNER JOIN subfolders sf ON f.parent_id = sf.id
            WHERE f.user_id = :user_id AND f.deleted_at IS NULL
        )
        SELECT id FROM subfolders
    """)
    result = await db.execute(query, {"root_id": folder_id, "user_id": user_id})
    return [int(item) for item in result.scalars().all()]


def get_media_file_size(media) -> int:
    if not media:
        return 0
    raw_size = getattr(media, "file_size", None)
    if raw_size:
        return int(raw_size)
    sizes = getattr(media, "sizes", None)
    if sizes:
        for item in reversed(list(sizes)):
            item_size = getattr(item, "file_size", None)
            if item_size:
                return int(item_size)
    return 0

def normalize_web_username(username: str) -> str:
    return re.sub(r"\s+", "", (username or "").lower())


def web_username_error(username: str) -> str | None:
    username = normalize_web_username(username)
    if not (settings.web_username_min_length <= len(username) <= settings.web_username_max_length):
        return f"Username must be {settings.web_username_min_length}-{settings.web_username_max_length} characters."
    if not USERNAME_RE.fullmatch(username):
        return "Use lowercase letters, numbers, dot, underscore, or dash. Username must start with a letter and end with a letter or number."
    if ".." in username or "--" in username or "__" in username:
        return "Username cannot contain repeated dot, dash, or underscore."
    if username in RESERVED_USERNAMES:
        return "This username is reserved."
    if any(part in username for part in BANNED_USERNAME_PARTS):
        return "This username is not allowed."
    return None


def is_valid_web_username(username: str) -> bool:
    return web_username_error(username) is None


def valid_web_password(password: str) -> str | None:
    normalized = normalize_password_secret(password)
    if len(normalized) < 8:
        return "Password must be at least 8 characters after spaces are removed."
    if len(normalized.encode("utf-8")) > 72:
        return "Password is too long for bcrypt. Use 72 bytes or fewer."
    return None


async def revoke_all_sessions_for_user(db, user: User) -> None:
    user.auth_version += 1
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    db.add(user)


async def delete_sensitive_message(message: Message) -> None:
    """Best-effort deletion for commands that contain passwords."""
    try:
        await message.delete()
    except Exception:
        # Bot may not be allowed to delete older messages in some clients/chats.
        pass


async def wait_for_secret_password(client, message: Message, purpose: str) -> str | None:
    """Ask for a password, delete the reply, and return it."""
    chat_id = message.chat.id
    if not begin_pending_action(chat_id):
        await message.reply("⚠️ Finish or cancel your current pending bot action first.")
        return None

    try:
        prompt = await message.reply(
            f"🔐 **{purpose}**\n\n"
            "Send the password in your next message within 90 seconds. I will delete it immediately if Telegram allows it.\n"
            "Password must be at least 8 characters. Spaces are removed automatically.\n"
            "Send /cancel to abort."
        )
        try:
            reply = await client.wait_for_message(chat_id=chat_id, timeout=90)
        except (asyncio.TimeoutError, TimeoutError):
            await prompt.reply("⌛ Password entry timed out. Run the command again when ready.")
            return None

        if reply.text and reply.text.strip().lower().startswith("/cancel"):
            await delete_sensitive_message(reply)
            await prompt.reply("❌ Cancelled.")
            return None

        password = normalize_password_secret(reply.text or "")
        await delete_sensitive_message(reply)
        return password or None
    finally:
        clear_pending_action(chat_id)


async def create_or_update_web_credential(
    db,
    user: User,
    telegram_id: int,
    username: str,
    password: str,
) -> tuple[bool, str | None]:
    """Create a permanent credential or reset password for the same username.

    Returns (did_reset_password, error_message).
    """
    username = normalize_web_username(username)
    username_error = web_username_error(username)
    if username_error:
        return False, username_error

    password_error = valid_web_password(password)
    if password_error:
        return False, password_error

    result = await db.execute(select(WebCredential).where(WebCredential.user_id == user.id))
    credential = result.scalar_one_or_none()

    if credential and credential.username != username:
        return False, (
            "Username changes are web-only. Your current username is "
            f"`{credential.username}`. Use Settings → Account on the web app to change it."
        )

    result = await db.execute(select(WebCredential).where(WebCredential.username == username))
    existing_username = result.scalar_one_or_none()
    if existing_username and existing_username.user_id != user.id:
        return False, "This username is already taken. Choose another one."

    password_hash = hash_password(password)
    did_reset = credential is not None
    if credential:
        credential.password_hash = password_hash
        credential.telegram_id = telegram_id
        credential.updated_at = utcnow()
        await revoke_all_sessions_for_user(db, user)
    else:
        credential = WebCredential(
            user_id=user.id,
            telegram_id=telegram_id,
            username=username,
            password_hash=password_hash,
        )
        db.add(credential)

    return did_reset, None


async def reset_existing_web_password(db, user: User, telegram_id: int, password: str) -> tuple[str | None, str | None]:
    """Reset password for an existing credential and revoke old sessions.

    Returns (username, error_message).
    """
    password_error = valid_web_password(password)
    if password_error:
        return None, password_error

    result = await db.execute(select(WebCredential).where(WebCredential.user_id == user.id))
    credential = result.scalar_one_or_none()
    if not credential:
        return None, "No web username exists yet. Create one first with /setlogin username."

    credential.password_hash = hash_password(password)
    credential.telegram_id = telegram_id
    credential.updated_at = utcnow()
    await revoke_all_sessions_for_user(db, user)
    return credential.username, None


def format_size(size_bytes: int) -> str:
    """Format bytes to human readable size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def format_duration(seconds: int) -> str:
    """Format seconds to human readable duration."""
    if not seconds:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"



IMAGE_EXTENSIONS = {
    "jpg", "jpeg", "png", "webp", "gif", "bmp", "avif", "svg", "ico",
    "tif", "tiff", "heic", "heif"
}


def get_file_extension(file_name: str | None) -> str:
    if not file_name or "." not in file_name:
        return ""
    return file_name.rsplit(".", 1)[-1].lower()


def is_image_media(file_name: str | None, mime_type: str | None) -> bool:
    mime = (mime_type or "").lower()
    return mime.startswith("image/") or get_file_extension(file_name) in IMAGE_EXTENSIONS


def get_best_photo(photo):
    # Pyrogram forks may expose message.photo as one Photo or as a list.
    if isinstance(photo, (list, tuple)):
        return photo[-1] if photo else None
    return photo


async def get_or_create_user(telegram_id: int, username: str = None, 
                             first_name: str = None, last_name: str = None) -> User:
    """Get or create a user in the database, safely handling concurrent bot updates."""
    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user:
            return user

        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        db.add(user)
        try:
            await db.commit()
            await db.refresh(user)
            return user
        except IntegrityError:
            # Another coroutine created the same Telegram user after our SELECT.
            await db.rollback()
            result = await db.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()
            if user:
                return user
            raise


async def create_one_time_web_login_url(telegram_id: int) -> str:
    """Create a one-use high-entropy web login code and return /auth?code=... URL."""
    alphabet = string.ascii_uppercase + string.digits
    expires_at = utcnow() + timedelta(minutes=settings.login_code_expiry_minutes)
    code_length = max(16, settings.web_login_code_length)

    async with async_session() as db:
        for _ in range(10):
            code = "".join(secrets.choice(alphabet) for _ in range(code_length))
            existing = await db.execute(select(LoginCode.id).where(LoginCode.code == code))
            if existing.scalar_one_or_none() is None:
                break
        else:
            raise RuntimeError("Could not generate a unique one-time login code")

        db.add(LoginCode(code=code, telegram_id=telegram_id, expires_at=expires_at))
        await db.commit()

    return f"{settings.web_base_url.rstrip('/')}/auth?code={code}"


def get_web_app_button(telegram_id: int, text: str = "🌐 Open Web") -> InlineKeyboardButton:
    """Create a safe Mini App button without putting JWTs in the URL."""
    return InlineKeyboardButton(text, web_app=WebAppInfo(url=settings.web_base_url.rstrip('/')))

# ============== Authorization Middleware ==============

@tg_client.on_message(filters.private, group=-2)
async def check_auth(client, message: Message):
    """Check if the user is authorized to use the bot."""
    auth_users = settings.auth_users
    if not auth_users:
        # Open to everyone
        return
    
    if message.from_user.id not in auth_users:
        # Ignore if it's a command we don't want to reply to (to avoid spamming unauthorized users)
        # But for /start, we should give a polite rejection
        if message.text and message.text.startswith("/start"):
            await message.reply(
                "🚫 **Access Restricted**\n\n"
                "Sorry, this bot is limited to authorized users only.\n"
                f"Your Telegram ID: `{message.from_user.id}`"
            )
        
        # Stop further processing of this message
        message.stop_propagation()

# ============== Command Handlers ==============

@tg_client.on_message(filters.command("start") & filters.private)
async def start_command(client, message: Message):
    """Welcome message and bot instructions. Also handles deep-linked login codes."""
    await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )
    
    # Check for deep-linked login codes (e.g. /start ABCDEF)
    if len(message.command) > 1:
        code_input = message.command[1].strip().upper()
        async with async_session() as db:
            result = await db.execute(select(LoginCode).where(LoginCode.code == code_input))
            login_code = result.scalar_one_or_none()
            
            if login_code:
                if login_code.expires_at > utcnow() and not login_code.telegram_id:
                    # Claim the code
                    login_code.telegram_id = message.from_user.id
                    await db.commit()
                    
                    await message.reply(
                        "✅ **Success!**\n"
                        "You have successfully logged in on your device.\n"
                        "You can now enjoy watching! 🍿"
                    )
                    return
                elif login_code.telegram_id:
                     await message.reply("⚠️ This code has already been used.")
                     return
                else:
                     await message.reply("❌ This code has expired.")
                     return

    await message.reply(
        "📺 **Welcome to TelePlay!**\n\n"
        "Your personal media streaming platform.\n"
        "Upload files here, stream anywhere!\n\n"
        
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 **QUICK START**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ Send any media file to upload\n"
        "2️⃣ Use /web to open web player\n"
        "3️⃣ Use /login on your TV app\n\n"
        
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📝 **COMMANDS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "/myfiles - Your files with IDs\n"
        "/file `<id>` - Manage a file\n"
        "/folders - Browse folders\n"
        "/newfolder `<name>` - New folder\n"
        "/setlogin `<user>` - Create permanent web login safely\n"
        "/resetpass - Reset web password and revoke sessions\n"
        "/myuser - Show web username/reset help\n"
        "/help - Full help guide\n\n"
        
        "💡 After uploading, you'll get the **File ID**\n"
        "Use `/file <id>` to rename, move, or delete.",
        reply_markup=InlineKeyboardMarkup([
            [get_web_app_button(message.from_user.id, "🌐 Open Web Interface")],
            [
                InlineKeyboardButton("📁 My Files", callback_data="show_files"),
                InlineKeyboardButton("📂 My Folders", callback_data="back_folders")
            ]
        ])
    )


@tg_client.on_message(filters.command("help") & filters.private)
async def help_command(client, message: Message):
    """Show help message."""
    await message.reply(
        "📖 **TelePlay Help**\n\n"
        
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📤 **UPLOADING FILES**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Simply send any video, audio, image or document to me.\n"
        "I'll save it to your library for streaming.\n\n"
        
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📋 **COMMANDS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        
        "**General:**\n"
        "• /start - Welcome message\n"
        "• /help - This help message\n"
        "• /web - Get one-time web login link\n"
        "• /login - Get/verify login code for TV/web\n"
        "• /setlogin `<user>` - Create web login safely\n"
        "• /resetpass - Reset password and revoke sessions\n"
        "• /myuser - Show your web username/reset help\n"
        "• /logout_all - Invalidate all active sessions\n\n"
        
        "**File Management:**\n"
        "• /myfiles - List your recent files with IDs\n"
        "• /file `<id>` - Manage a specific file\n"
        "  ↳ Rename, Move, Delete, Open Web\n\n"
        
        "**Folder Management:**\n"
        "• /folders - Browse all folders\n"
        "• /newfolder `<name>` - Create a folder\n"
        "• /deletefolder `<name>` - Delete a folder\n\n"
        
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎛 **INTERACTIVE ACTIONS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "When you tap buttons, I'll ask for input:\n"
        "• **Rename** - Send new name (60s timeout)\n"
        "• **Create Folder** - Send folder name\n"
        "• **Delete** - Tap confirm or cancel\n"
        "• **Move** - Select destination folder\n\n"
        "💡 Send /cancel to abort any action\n\n"
        
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📁 **SUPPORTED FILES**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• 🎬 Videos: MP4, MKV, AVI, MOV, WEBM\n"
        "• 🎵 Audio: MP3, FLAC, AAC, OGG, WAV\n"
        "• 🖼 Images: JPG, PNG, GIF, WEBP\n"
        "• 📄 Documents: PDF, TXT, DOCX, etc.\n"
        "• ⚠️ Max size: 2GB per file\n\n"
        
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📺 **TV & WEB STREAMING**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• Use /web to get a secure link\n"
        "• Use /login on TV app to connect\n"
        "• Watch progress syncs across devices\n"
    )


@tg_client.on_message(filters.command("myfiles") & filters.private)
async def myfiles_command(client, message: Message):
    """List user's recent files."""
    async with async_session() as db:
        result = await db.execute(
            select(File)
            .where(File.user_id == (
                select(User.id).where(User.telegram_id == message.from_user.id).scalar_subquery()
            ))
            .order_by(File.created_at.desc())
            .limit(10)
        )
        files = result.scalars().all()
    
    if not files:
        await message.reply(
            "📭 You haven't uploaded any files yet.\n\n"
            "Send me a video, audio, or document to get started!"
        )
        return
    
    text = "📁 **Your Recent Files:**\n\n"
    
    for f in files:
        emoji = {"video": "🎬", "audio": "🎵", "document": "📄", "image": "🖼"}.get(f.file_type, "📎")
        text += f"{emoji} `{f.id}` | {f.file_name}\n   └ {format_size(f.file_size)}"
        if f.duration:
            text += f" • {format_duration(f.duration)}"
        text += "\n\n"
    
    text += "💡 Use /file <id> to manage a file"
    
    await message.reply(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📁 My Folders", callback_data="back_folders")],
            [get_web_app_button(message.from_user.id, "🌐 Open Web")]
        ])
    )


@tg_client.on_message(filters.command("folders") & filters.private)
async def folders_command(client, message: Message):
    """Show folder structure."""
    async with async_session() as db:
        # Get user
        user_result = await db.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        
        if not user:
            await message.reply("Please use /start first.")
            return
        
        # Get root folders
        result = await db.execute(
            select(Folder)
            .where(Folder.user_id == user.id, Folder.parent_id.is_(None))
            .order_by(Folder.name)
        )
        folders = result.scalars().all()
    
    if not folders:
        await message.reply(
            "📁 You don't have any folders yet.\n\n"
            "Create one with /newfolder <name>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Create Folder", callback_data="create_folder")]
            ])
        )
        return
    
    buttons = []
    for f in folders:
        buttons.append([
            InlineKeyboardButton(f"📂 {f.name}", callback_data=f"folder:{f.id}")
        ])
    buttons.append([InlineKeyboardButton("➕ Create Folder", callback_data="create_folder")])
    
    await message.reply(
        "📁 **Your Folders:**",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


@tg_client.on_message(filters.command("newfolder") & filters.private)
async def newfolder_command(client, message: Message):
    """Create a new folder."""
    if len(message.command) < 2:
        await message.reply("Usage: /newfolder <folder_name>")
        return
    
    folder_name = clean_bot_folder_name(" ".join(message.command[1:]))
    if not folder_name:
        await message.reply("❌ Invalid folder name.")
        return
    
    async with async_session() as db:
        user = await get_bot_user(db, message.from_user.id)
        if not user:
            await message.reply("Please use /start first.")
            return
        
        if await bot_folder_name_exists(db, user.id, folder_name, parent_id=None):
            await message.reply(f"❌ Folder **{folder_name}** already exists.")
            return
        
        folder = Folder(user_id=user.id, name=folder_name)
        db.add(folder)
        await db.commit()
    
    await message.reply(f"✅ Folder **{folder_name}** created!")


@tg_client.on_message(filters.command("setlogin") & filters.private)
async def setlogin_command(client, message: Message):
    """Create the permanent web credential, or reset password only for the same username."""
    parts = (message.text or "").split(maxsplit=2)

    await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )

    if len(parts) < 2:
        await message.reply(
            "🔐 **Create Web Login**\n\n"
            "Safer usage:\n"
            "`/setlogin username`\n"
            "Then send the password in the next message. I delete the password message if Telegram allows it.\n\n"
            "Rules:\n"
            f"• Username: {settings.web_username_min_length}-{settings.web_username_max_length} chars\n"
            "• Username chars: lowercase letters, numbers, dot, underscore, dash\n"
            "• Username must start with a letter and end with a letter or number\n"
            "• Password: at least 8 characters; spaces are removed automatically\n\n"
            "Important:\n"
            "• Each Telegram account gets one permanent web username\n"
            "• Username changes are web-only so availability can be checked live\n"
            "• Reusing the same username resets the password and logs out old sessions"
        )
        return

    username = normalize_web_username(parts[1])

    if len(parts) >= 3:
        await delete_sensitive_message(message)
        await message.reply(
            "❌ One-line `/setlogin username password` is disabled for security and will not be processed.\n\n"
            "Use `/setlogin username`, then send the password only after I ask."
        )
        return

    username_error = web_username_error(username)
    if username_error:
        await message.reply(f"❌ {username_error}")
        return

    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if not user:
            await message.reply("Please use /start first.")
            return

        existing_result = await db.execute(select(WebCredential).where(WebCredential.user_id == user.id))
        existing_credential = existing_result.scalar_one_or_none()
        if existing_credential and existing_credential.username != username:
            await message.reply(
                f"❌ You already have a web username: `{existing_credential.username}`.\n\n"
                "Username changes are only available in web Settings because availability must be checked live.\n\n"
                f"To reset password from bot, use `/setlogin {existing_credential.username}` or `/resetpass`."
            )
            return

        password = await wait_for_secret_password(client, message, "Set Web Login Password")
        if password is None:
            return

        did_reset, error = await create_or_update_web_credential(
            db, user, message.from_user.id, username, password
        )
        if error:
            await message.reply(f"❌ {error}")
            return

        await db.commit()

    if did_reset:
        await message.reply(
            "✅ **Password reset!**\n\n"
            f"Username: `{username}`\n"
            "Old web sessions were revoked. Please sign in again."
        )
    else:
        await message.reply(
            "✅ **Permanent web login created!**\n\n"
            f"Username: `{username}`\n"
            "Password was hashed with bcrypt. Use the web login page to sign in."
        )


@tg_client.on_message(filters.command("resetpass") & filters.private)
async def resetpass_command(client, message: Message):
    """Reset the existing web password without allowing username changes."""
    parts = (message.text or "").split(maxsplit=1)

    await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )

    if len(parts) >= 2:
        await delete_sensitive_message(message)
        await message.reply(
            "❌ One-line `/resetpass password` is disabled for security and will not be processed.\n\n"
            "Use `/resetpass`, then send the password only after I ask."
        )
        return

    password = await wait_for_secret_password(client, message, "Reset Web Password")
    if password is None:
        return

    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if not user:
            await message.reply("Please use /start first.")
            return

        username, error = await reset_existing_web_password(db, user, message.from_user.id, password)
        if error:
            await message.reply(f"❌ {error}")
            return

        await db.commit()

    await message.reply(
        "✅ **Password reset!**\n\n"
        f"Username: `{username}`\n"
        "All old sessions were revoked. Please sign in again."
    )


@tg_client.on_message(filters.command("myuser") & filters.private)
async def myuser_command(client, message: Message):
    """Show the current permanent web username."""
    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if not user:
            await message.reply("Please use /start first.")
            return

        result = await db.execute(select(WebCredential).where(WebCredential.user_id == user.id))
        credential = result.scalar_one_or_none()

    if not credential:
        await message.reply(
            "🔐 **No web username set yet.**\n\n"
            "Create one safely with:\n"
            "`/setlogin username`\n"
            "Then send the password in the next message.",
            reply_markup=InlineKeyboardMarkup([[get_web_app_button(message.from_user.id, "🌐 Open Web")]])
        )
        return

    await message.reply(
        "👤 **Your Web Login**\n\n"
        f"Username: `{credential.username}`\n\n"
        "Forgot password? Reset it by sending:\n"
        "`/resetpass`\n"
        "Then send your new password in the next message.\n\n"
        "Your web username is permanent. Username changes are web-only so availability can be checked live.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 Reset Password Help", callback_data="web_login_reset_help")],
            [get_web_app_button(message.from_user.id, "🌐 Open Web")],
        ])
    )


@tg_client.on_message(filters.command("clearlogin") & filters.private)
async def clearlogin_command(client, message: Message):
    """Credentials are permanent; this command is kept as a safe deprecation notice."""
    await message.reply(
        "🔒 **Web login credentials are permanent now.**\n\n"
        "I will not delete your username/password mapping from Telegram.\n"
        "Use `/resetpass` to reset your password, or `/logout_all` to revoke active sessions."
    )


@tg_client.on_message(filters.command("web") & filters.private)
async def web_command(client, message: Message):
    """Get a one-time web login link without exposing a JWT in the URL."""
    await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )

    web_url = await create_one_time_web_login_url(message.from_user.id)

    await message.reply(
        "🌐 **One-Time Web Login**\n\n"
        "Click the link below to access your files:\n"
        f"👉 {web_url}\n\n"
        f"__(One use only. Expires in {settings.login_code_expiry_minutes} minutes)__"
    )


@tg_client.on_message(filters.command("login") & filters.private)
async def login_command(client, message: Message):
    """
    Handle login command.
    Usage:
    /login <CODE> - Link TV/Web session
    /login - Generate code to enter on device
    """
    await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )

    # Check if code is provided (TV/Web -> User flow)
    if len(message.command) > 1:
        code_input = message.command[1].strip().upper()
        
        async with async_session() as db:
            result = await db.execute(select(LoginCode).where(LoginCode.code == code_input))
            login_code = result.scalar_one_or_none()
            
            if not login_code:
                await message.reply("❌ **Invalid code.**\nPlease check the code displayed on your TV.")
                return
            
            if login_code.expires_at < utcnow():
                await message.reply("❌ **Code expired.**\nPlease generate a new one on your TV.")
                return
                
            if login_code.telegram_id:
                await message.reply("❌ **Code already used.**")
                return

            # Claim the code
            login_code.telegram_id = message.from_user.id
            await db.commit()
            
            await message.reply(
                "✅ **Success!**\n"
                "You have successfully logged in on your TV.\n"
                "You can now put your phone away and enjoy watching! 🍿"
            )
        return

    # Use secrets for cryptographically strong random number generation
    alphabet = string.ascii_uppercase + string.digits
    code = ''.join(secrets.choice(alphabet) for _ in range(settings.login_code_length))
    
    async with async_session() as db:
        # Save code
        login_code = LoginCode(
            code=code,
            telegram_id=message.from_user.id,
            expires_at=utcnow() + timedelta(minutes=settings.login_code_expiry_minutes)
        )
        db.add(login_code)
        await db.commit()
    
    await message.reply(
        "🔑 **Your Login Code:**\n\n"
        f"`{code}`\n\n"
        "Enter this code on the login screen.\n"
        f"__(Expires in {settings.login_code_expiry_minutes} minutes)__"
    )

    return

@tg_client.on_message(filters.command("logout_all") & filters.private)
async def logout_all_command(client, message: Message):
    """
    Invalidate all active sessions for the current user.
    """
    await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )
    
    await message.reply(
        "⚠️ **Confirm Global Logout**\n\n"
        "Are you sure you want to log out from **ALL** devices?\n"
        "This will invalidate your session on:\n"
        "• Web App\n"
        "• Android TV\n"
        "• Mobile App",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, Logout", callback_data="logout_all_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="logout_all_cancel")
            ]
        ])
    )

# ============== File Handler ==============

@tg_client.on_message(filters.private & (filters.video | filters.audio | filters.document | filters.photo))
async def handle_file(client, message: Message):
    """Handle uploaded files - forward to channel and save to DB."""
    # Get or create user
    user = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )
    
    # Determine file type and extract metadata
    if message.video:
        media = message.video
        file_type = "video"
    elif message.audio:
        media = message.audio
        file_type = "audio"
    elif message.document:
        media = message.document
        file_type = "image" if is_image_media(getattr(media, "file_name", None), getattr(media, "mime_type", None)) else "document"
    elif message.photo:
        media = get_best_photo(message.photo)
        if not media:
            return await message.reply("❌ Could not read this photo.")
        file_type = "image"
    else:
        return

    duplicate_key = (int(message.chat.id), int(message.id))
    keep_duplicate = duplicate_key in _duplicate_upload_overrides
    _duplicate_upload_overrides.discard(duplicate_key)

    # file_unique_id is stable for the same Telegram media and does not expose
    # another user's content. Stop before forwarding so a warning does not
    # consume storage unless the user explicitly keeps another copy.
    if not keep_duplicate and getattr(media, "file_unique_id", None):
        async with async_session() as db:
            duplicate_result = await db.execute(
                select(File)
                .where(
                    File.user_id == user.id,
                    File.file_unique_id == media.file_unique_id,
                )
                .order_by(File.deleted_at.is_not(None), File.created_at.desc())
                .execution_options(include_deleted=True)
            )
            duplicate = duplicate_result.scalars().first()

        if duplicate:
            location = "Recycle Bin" if duplicate.deleted_at else "your library"
            raw_name = getattr(media, "file_name", None) or f"{file_type}_{message.id}"
            await message.reply(
                "⚠️ **Duplicate detected**\n\n"
                f"**{sanitize_filename(raw_name)}** already exists in {location}.\n"
                f"Existing file: **{duplicate.file_name}**\n"
                f"File ID: `{duplicate.id}` · Size: {format_size(duplicate.file_size)}\n\n"
                "No Telegram storage has been used for this copy yet.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "Keep another copy",
                            callback_data=f"keepduplicate:{message.id}",
                        ),
                        InlineKeyboardButton("Cancel", callback_data="cancelduplicate"),
                    ]
                ]),
            )
            return
    
    status_msg = await message.reply("📥 Processing file...")
    
    try:
        # Forward to storage channel
        forwarded = await forward_to_storage_channel(message)
        
        # Extract file info. Photos sometimes omit file_size on the original update,
        # so fall back to the forwarded storage message before storing 0.
        raw_filename = getattr(media, "file_name", None) or (f"{file_type}_{message.id}.jpg" if file_type == "image" else f"{file_type}_{message.id}")
        mime_type = resolve_media_type(
            raw_filename,
            getattr(media, "mime_type", None),
            file_type,
        )
        thumbs = getattr(media, "thumbs", None)
        file_size = get_media_file_size(media)
        if file_size <= 0 and file_type == "image":
            forwarded_photo = get_best_photo(getattr(forwarded, "photo", None))
            file_size = get_media_file_size(forwarded_photo)
        file_info = {
            "file_id": media.file_id,
            "file_unique_id": media.file_unique_id,
            "file_name": sanitize_filename(raw_filename),
            "file_size": file_size,
            "mime_type": mime_type,
            "duration": getattr(media, "duration", None),
            "width": getattr(media, "width", None),
            "height": getattr(media, "height", None),
            # Never mark the original image as its own thumbnail. When Telegram
            # has no small thumbnail, the API creates an on-demand cached WebP.
            "thumbnail_file_id": thumbs[0].file_id if thumbs else None,
        }
        
        # Save to database
        async with async_session() as db:
            file = File(
                user_id=user.id,
                channel_message_id=forwarded.id,
                file_type=file_type,
                **file_info
            )
            db.add(file)
            await db.commit()
            await db.refresh(file)
        
        # Build response
        emoji = {"video": "🎬", "audio": "🎵", "document": "📄", "image": "🖼"}.get(file_type, "📎")
        
        response = (
            f"✅ **File saved!**\n\n"
            f"{emoji} **{file_info['file_name']}**\n"
            f"🆔 File ID: `{file.id}`\n"
            f"📦 Size: {format_size(file_info['file_size'])}\n"
            f"🎭 Type: {file_type}\n"
        )
        
        if file_info['duration']:
            response += f"⏱ Duration: {format_duration(file_info['duration'])}\n"
        
        response += f"\n📁 Folder: / (root)\n\n"
        response += f"💡 Use `/file {file.id}` to manage this file"
        
        await status_msg.edit(
            response,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✏️ Rename", callback_data=f"renamefile:{file.id}"),
                    InlineKeyboardButton("📂 Move", callback_data=f"move:{file.id}"),
                ],
                [
                    InlineKeyboardButton("🗑 Delete", callback_data=f"delfile:{file.id}"),
                    InlineKeyboardButton("🔗 Share", callback_data=f"sharefile:{file.id}"),
                ],
            ])
        )
        
    except Exception as e:
        await status_msg.edit(f"❌ Failed to process file: {str(e)}")


# ============== Callback Query Handlers ==============

@tg_client.on_callback_query()
async def handle_callback(client, callback: CallbackQuery):
    """Handle inline button callbacks."""
    data = callback.data
    
    if data.startswith("keepduplicate:"):
        source_message_id = int(data.split(":", 1)[1])
        chat_id = int(callback.message.chat.id)
        original_message = await client.get_messages(chat_id, source_message_id)
        if not original_message or not (
            original_message.video
            or original_message.audio
            or original_message.document
            or original_message.photo
        ):
            await callback.answer("Original upload is no longer available", show_alert=True)
            return

        _duplicate_upload_overrides.add((chat_id, source_message_id))
        await callback.message.edit(
            "📥 **Keeping another copy…**\n\nThe upload result will appear below."
        )
        await callback.answer("Duplicate upload allowed")
        await handle_file(client, original_message)

    elif data == "cancelduplicate":
        await callback.message.edit(
            "✅ **Duplicate upload cancelled**\n\nThe existing file was kept and no extra Telegram storage was used."
        )
        await callback.answer("Cancelled")

    elif data == "logout_all_confirm":
        # Perform global logout
        async with async_session() as db:
            result = await db.execute(select(User).where(User.telegram_id == callback.from_user.id))
            user = result.scalar_one_or_none()
            
            if user:
                await revoke_all_sessions_for_user(db, user)
                await db.commit()
                await callback.message.edit(
                    "✅ **All sessions invalidated!**\n"
                    "You have been successfully logged out from all web, TV, and mobile devices."
                )
            else:
                await callback.answer("User not found", show_alert=True)
        await callback.answer()
        
    elif data == "logout_all_cancel":
        # Cancel logout
        await callback.message.edit("❌ **Global logout cancelled.**")
        await callback.answer()

    elif data == "get_web_link":
        # Fallback for old messages - create a one-use code link instead of a JWT URL.
        web_url = await create_one_time_web_login_url(callback.from_user.id)
        await callback.message.reply(
            f"🌐 **One-Time Web Login**\n\n"
            f"👉 {web_url}\n\n"
            f"__(One use only. Expires in {settings.login_code_expiry_minutes} minutes)__\n\n"
            "💡 Tap the button below to open the web app directly:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Open One-Time Login", url=web_url)]
            ])
        )
        await callback.answer()

    elif data == "web_login_reset_help":
        await callback.message.reply(
            "🔐 **Reset Web Password**\n\n"
            "Send this command with your new password:\n"
            "`/resetpass`\n\n"
            "I will delete that command message if Telegram allows it. Old sessions will be revoked."
        )
        await callback.answer()
        
    elif data == "show_files":
        # Show recent files similar to /myfiles command
        async with async_session() as db:
            result = await db.execute(
                select(File)
                .where(File.user_id == (
                    select(User.id).where(User.telegram_id == callback.from_user.id).scalar_subquery()
                ))
                .order_by(File.created_at.desc())
                .limit(10)
            )
            files = result.scalars().all()
        
        if not files:
            await callback.message.reply(
                "📭 You haven't uploaded any files yet.\n\n"
                "Send me a video, audio, or document to get started!"
            )
            await callback.answer()
            return
        
        text = "📁 **Your Recent Files:**\n\n"
        
        for f in files:
            emoji = {"video": "🎬", "audio": "🎵", "document": "📄", "image": "🖼"}.get(f.file_type, "📎")
            text += f"{emoji} `{f.id}` | {f.file_name}\n   └ {format_size(f.file_size)}\n\n"
        
        text += "💡 Use /file <id> to manage a file"
        
        await callback.message.reply(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📂 My Folders", callback_data="back_folders")],
                [get_web_app_button(callback.from_user.id, "🌐 Open Web")]
            ])
        )
        await callback.answer()
        
    elif data == "create_folder":
        chat_id = callback.message.chat.id
        if not begin_pending_action(chat_id):
            await callback.answer("Finish or cancel the current pending action first.", show_alert=True)
            return

        try:
            await callback.message.reply(
                "📁 **Create New Folder**\n\n"
                "Send me the folder name:\n"
                "__(or send /cancel to abort)__"
            )
            await callback.answer()
            
            try:
                reply = await client.wait_for_message(chat_id=chat_id, timeout=60)
                
                if reply.text and reply.text.startswith("/cancel"):
                    await reply.reply("❌ Folder creation cancelled.")
                    return
                
                folder_name = clean_bot_folder_name(reply.text if reply.text else None)
                if not folder_name:
                    await reply.reply("❌ Invalid folder name.")
                    return
                
                async with async_session() as db:
                    user = await get_bot_user(db, callback.from_user.id)
                    if not user:
                        await reply.reply("Please use /start first.")
                        return
                    
                    if await bot_folder_name_exists(db, user.id, folder_name, parent_id=None):
                        await reply.reply(f"❌ Folder **{folder_name}** already exists.")
                        return
                    
                    folder = Folder(user_id=user.id, name=folder_name)
                    db.add(folder)
                    await db.commit()
                
                await reply.reply(f"✅ Folder **{folder_name}** created!")
                
            except Exception as e:
                if "timeout" in str(e).lower():
                    await callback.message.reply("⏱ Timed out. Please try again.")
                else:
                    await callback.message.reply(f"❌ Error: {str(e)}")
        finally:
            clear_pending_action(chat_id)
        
    elif data.startswith("folder:"):
        folder_id = int(data.split(":")[1])
        async with async_session() as db:
            user = await get_bot_user(db, callback.from_user.id)
            if not user:
                await callback.answer("Please use /start first", show_alert=True)
                return

            folder = await get_owned_folder(db, user.id, folder_id)
            if not folder:
                await callback.answer("Folder not found", show_alert=True)
                return
            
            files_result = await db.execute(
                select(File)
                .where(File.folder_id == folder_id, File.user_id == user.id)
                .limit(10)
            )
            files = files_result.scalars().all()
        
        text = f"📂 **{folder.name}**\n\n"
        
        if not files:
            text += "__No files in this folder__\n"
        else:
            for f in files:
                emoji = {"video": "🎬", "audio": "🎵", "document": "📄", "image": "🖼"}.get(f.file_type, "📎")
                text += f"{emoji} `{f.id}` | {f.file_name}\n   └ {format_size(f.file_size)}\n\n"
            text += "💡 Use /file <id> to manage a file"
        
        # Add folder management buttons
        buttons = [
            [
                InlineKeyboardButton("✏️ Rename", callback_data=f"renamefolder:{folder_id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"delfolder:{folder_id}"),
            ],
            [InlineKeyboardButton("« Back to Folders", callback_data="back_folders")]
        ]
        
        await callback.message.edit(text, reply_markup=InlineKeyboardMarkup(buttons))
        
    elif data == "back_folders":
        # Go back to folder list
        async with async_session() as db:
            user_result = await db.execute(
                select(User).where(User.telegram_id == callback.from_user.id)
            )
            user = user_result.scalar_one_or_none()
            
            if not user:
                await callback.answer("Please use /start first", show_alert=True)
                return
            
            result = await db.execute(
                select(Folder)
                .where(Folder.user_id == user.id, Folder.parent_id.is_(None))
                .order_by(Folder.name)
            )
            folders = result.scalars().all()
        
        if not folders:
            await callback.message.edit(
                "📁 You don't have any folders yet.\n\n"
                "Create one with /newfolder <name>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Create Folder", callback_data="create_folder")]
                ])
            )
        else:
            buttons = []
            for f in folders:
                buttons.append([
                    InlineKeyboardButton(f"📂 {f.name}", callback_data=f"folder:{f.id}")
                ])
            buttons.append([InlineKeyboardButton("➕ Create Folder", callback_data="create_folder")])
            
            await callback.message.edit("📁 **Your Folders:**", reply_markup=InlineKeyboardMarkup(buttons))
        
        await callback.answer()
        
    elif data.startswith("move:"):
        file_id = int(data.split(":")[1])
        
        async with async_session() as db:
            user = await get_bot_user(db, callback.from_user.id)
            if not user:
                await callback.answer("Please use /start first", show_alert=True)
                return

            file = await get_owned_file(db, user.id, file_id)
            if not file:
                await callback.answer("File not found", show_alert=True)
                return
            
            folders_result = await db.execute(
                select(Folder).where(Folder.user_id == user.id).order_by(Folder.name)
            )
            folders = folders_result.scalars().all()
        
        if not folders:
            await callback.answer("No folders yet. Create one with /newfolder", show_alert=True)
            return
        
        buttons = []
        for f in folders:
            buttons.append([
                InlineKeyboardButton(f"📂 {f.name}", callback_data=f"moveto:{file_id}:{f.id}")
            ])
        buttons.append([InlineKeyboardButton("📁 Root (no folder)", callback_data=f"moveto:{file_id}:0")])
        
        await callback.message.edit_reply_markup(InlineKeyboardMarkup(buttons))
        await callback.answer()
        
    elif data.startswith("moveto:"):
        _, file_id, folder_id = data.split(":")
        file_id = int(file_id)
        folder_id = int(folder_id) if folder_id != "0" else None
        
        async with async_session() as db:
            user = await get_bot_user(db, callback.from_user.id)
            if not user:
                await callback.answer("Please use /start first", show_alert=True)
                return

            file = await get_owned_file(db, user.id, file_id)
            if not file:
                await callback.answer("File not found", show_alert=True)
                return

            if folder_id is not None:
                target_folder = await get_owned_folder(db, user.id, folder_id)
                if not target_folder:
                    await callback.answer("Target folder not found", show_alert=True)
                    return

            file.folder_id = folder_id
            await db.commit()
            await callback.answer("✅ File moved!", show_alert=True)
                
    # ============== New File Management Callbacks ==============
    
    elif data.startswith("renamefile:"):
        file_id = int(data.split(":")[1])
        
        async with async_session() as db:
            user = await get_bot_user(db, callback.from_user.id)
            if not user:
                await callback.answer("Please use /start first", show_alert=True)
                return
            file = await get_owned_file(db, user.id, file_id)
            if not file:
                await callback.answer("File not found", show_alert=True)
                return
            current_name = file.file_name
        
        chat_id = callback.message.chat.id
        if not begin_pending_action(chat_id):
            await callback.answer("Finish or cancel the current pending action first.", show_alert=True)
            return

        try:
            await callback.message.reply(
                f"✏️ **Rename File**\n\n"
                f"Current name: `{current_name}`\n\n"
                "Send me the new name:\n"
                "__(or send /cancel to abort)__"
            )
            await callback.answer()
            
            try:
                reply = await client.wait_for_message(chat_id=chat_id, timeout=60)
                
                if reply.text and reply.text.startswith("/cancel"):
                    await reply.reply("❌ Rename cancelled.")
                    return
                
                new_name = sanitize_filename(reply.text or "").strip()
                if not new_name:
                    await reply.reply("❌ Invalid name.")
                    return
                
                async with async_session() as db:
                    user = await get_bot_user(db, callback.from_user.id)
                    if not user:
                        await reply.reply("Please use /start first.")
                        return
                    file = await get_owned_file(db, user.id, file_id)
                    if file:
                        file.file_name = new_name
                        await db.commit()
                        await reply.reply(f"✅ File renamed to **{new_name}**")
                    else:
                        await reply.reply("❌ File not found.")
                        
            except Exception as e:
                if "timeout" in str(e).lower():
                    await callback.message.reply("⏱ Timed out. Please try again.")
        finally:
            clear_pending_action(chat_id)
    
    elif data.startswith("delfile:"):
        file_id = int(data.split(":")[1])
        
        async with async_session() as db:
            user = await get_bot_user(db, callback.from_user.id)
            if not user:
                await callback.answer("Please use /start first", show_alert=True)
                return
            file = await get_owned_file(db, user.id, file_id)
            if not file:
                await callback.answer("File not found", show_alert=True)
                return
                
            file_name = file.file_name
        
        # Ask for confirmation
        await callback.message.edit(
            f"🗑 **Delete File?**\n\n"
            f"Are you sure you want to delete:\n"
            f"`{file_name}`\n\n"
            "You can restore it from the web Recycle Bin while retention is enabled.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirmdelfile:{file_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data="canceldel"),
                ]
            ])
        )
        await callback.answer()
        
    elif data.startswith("confirmdelfile:"):
        file_id = int(data.split(":")[1])
        
        async with async_session() as db:
            user = await get_bot_user(db, callback.from_user.id)
            if not user:
                await callback.answer("Please use /start first", show_alert=True)
                return
            file = await get_owned_file(db, user.id, file_id)
            if not file:
                await callback.answer("File not found", show_alert=True)
                return
            
            file_name = file.file_name
            deleted_count, recycled = await trash_files(db, user.id, [file_id])
            await db.commit()

        destination = "moved to Recycle Bin" if recycled else "permanently deleted"
        await callback.message.edit(f"✅ File **{file_name}** {destination}!")
        await callback.answer(destination.capitalize(), show_alert=True)
        
    elif data.startswith("renamefolder:"):
        folder_id = int(data.split(":")[1])
        
        async with async_session() as db:
            user = await get_bot_user(db, callback.from_user.id)
            if not user:
                await callback.answer("Please use /start first", show_alert=True)
                return
            folder = await get_owned_folder(db, user.id, folder_id)
            if not folder:
                await callback.answer("Folder not found", show_alert=True)
                return
            current_name = folder.name
        
        chat_id = callback.message.chat.id
        if not begin_pending_action(chat_id):
            await callback.answer("Finish or cancel the current pending action first.", show_alert=True)
            return

        try:
            await callback.message.reply(
                f"✏️ **Rename Folder**\n\n"
                f"Current name: `{current_name}`\n\n"
                "Send me the new name:\n"
                "__(or send /cancel to abort)__"
            )
            await callback.answer()
            
            try:
                reply = await client.wait_for_message(chat_id=chat_id, timeout=60)
                
                if reply.text and reply.text.startswith("/cancel"):
                    await reply.reply("❌ Rename cancelled.")
                    return
                
                new_name = clean_bot_folder_name(reply.text if reply.text else None)
                if not new_name:
                    await reply.reply("❌ Invalid name.")
                    return
                
                async with async_session() as db:
                    user = await get_bot_user(db, callback.from_user.id)
                    if not user:
                        await reply.reply("Please use /start first.")
                        return
                    folder = await get_owned_folder(db, user.id, folder_id)
                    if not folder:
                        await reply.reply("❌ Folder not found.")
                        return

                    if await bot_folder_name_exists(
                        db, user.id, new_name, folder.parent_id, exclude_folder_id=folder.id
                    ):
                        await reply.reply(f"❌ Folder **{new_name}** already exists here.")
                        return

                    folder.name = new_name
                    await db.commit()
                    await reply.reply(f"✅ Folder renamed to **{new_name}**")
                        
            except Exception as e:
                if "timeout" in str(e).lower():
                    await callback.message.reply("⏱ Timed out. Please try again.")
        finally:
            clear_pending_action(chat_id)
    
    elif data.startswith("delfolder:"):
        folder_id = int(data.split(":")[1])
        
        async with async_session() as db:
            user = await get_bot_user(db, callback.from_user.id)
            if not user:
                await callback.answer("Please use /start first", show_alert=True)
                return
            folder = await get_owned_folder(db, user.id, folder_id)
            if not folder:
                await callback.answer("Folder not found", show_alert=True)
                return
            
            folder_name = folder.name
            folder_ids = await get_descendant_folder_ids_for_user(db, folder_id, user.id)
            files_count = await db.execute(
                select(func.count(File.id)).where(
                    File.user_id == user.id,
                    File.folder_id.in_(folder_ids),
                )
            )
            count = files_count.scalar() or 0
        
        # Ask for confirmation
        text = (
            f"🗑 **Delete Folder?**\n\n"
            f"Folder: **{folder_name}**\n"
        )
        
        if count > 0:
            text += f"\nThis folder contains **{count} file(s)**. Its hierarchy will be kept in Recycle Bin."
        
        await callback.message.edit(
            text,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirmdelfolder:{folder_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data="back_folders"),
                ]
            ])
        )
        await callback.answer()
        
    elif data.startswith("confirmdelfolder:"):
        folder_id = int(data.split(":")[1])
        
        async with async_session() as db:
            user = await get_bot_user(db, callback.from_user.id)
            if not user:
                await callback.answer("Please use /start first", show_alert=True)
                return
            folder = await get_owned_folder(db, user.id, folder_id)
            if not folder:
                await callback.answer("Folder not found", show_alert=True)
                return
            
            folder_name = folder.name
            _, _, recycled = await trash_folder(db, user.id, folder_id)
            await db.commit()

        destination = "moved to Recycle Bin" if recycled else "permanently deleted"
        await callback.message.edit(f"✅ Folder **{folder_name}** {destination}!")
        await callback.answer(destination.capitalize(), show_alert=True)
    
    elif data.startswith("sharefile:"):
        file_id = int(data.split(":")[1])
        
        async with async_session() as db:
            # Verify ownership
            user_result = await db.execute(
                select(User).where(User.telegram_id == callback.from_user.id)
            )
            user = user_result.scalar_one_or_none()
            
            if not user:
                await callback.answer("Please use /start first", show_alert=True)
                return
            
            result = await db.execute(
                select(File).where(File.id == file_id, File.user_id == user.id)
            )
            file = result.scalar_one_or_none()
            
            if not file:
                await callback.answer("File not found", show_alert=True)
                return
            
            # Generate public hash
            file.public_hash = secrets.token_hex(16)
            await db.commit()
            await db.refresh(file)
            
            public_url = f"{settings.web_base_url}{build_public_stream_path(file)}"
        
        await callback.message.reply(
            f"🔗 **Public Link Generated!**\n\n"
            f"Stream URL:\n`{public_url}`\n\n"
            "Anyone with this link can stream the file.\n"
            "Use the button below to revoke access.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Unshare", callback_data=f"unsharefile:{file_id}")]
            ])
        )
        await callback.answer("Public link created!", show_alert=True)
    
    elif data.startswith("unsharefile:"):
        file_id = int(data.split(":")[1])
        
        async with async_session() as db:
            # Verify ownership
            user_result = await db.execute(
                select(User).where(User.telegram_id == callback.from_user.id)
            )
            user = user_result.scalar_one_or_none()
            
            if not user:
                await callback.answer("Please use /start first", show_alert=True)
                return
            
            result = await db.execute(
                select(File).where(File.id == file_id, File.user_id == user.id)
            )
            file = result.scalar_one_or_none()
            
            if not file:
                await callback.answer("File not found", show_alert=True)
                return
            
            file.public_hash = None
            await db.commit()
        
        await callback.message.reply(
            "🔗 **Public link revoked!**\n\n"
            "The file is no longer publicly accessible.\n"
            "You can generate a new link anytime.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Share", callback_data=f"sharefile:{file_id}")]
            ])
        )
        await callback.answer("Public link revoked!", show_alert=True)
    
    elif data == "canceldel":
        await callback.message.edit("❌ Deletion cancelled.")
        await callback.answer()


# ============== File Action Command ==============

@tg_client.on_message(filters.command("file") & filters.private)
async def file_command(client, message: Message):
    """Manage a specific file by ID."""
    if len(message.command) < 2:
        await message.reply("Usage: /file <file_id>")
        return
    
    try:
        file_id = int(message.command[1])
    except ValueError:
        await message.reply("❌ Invalid file ID.")
        return
    
    async with async_session() as db:
        # Get user
        user_result = await db.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        
        if not user:
            await message.reply("Please use /start first.")
            return
        
        # Get file
        result = await db.execute(
            select(File).where(File.id == file_id, File.user_id == user.id)
        )
        file = result.scalar_one_or_none()
    
    if not file:
        await message.reply("❌ File not found or you don't have access.")
        return
    
    emoji = {"video": "🎬", "audio": "🎵", "document": "📄", "image": "🖼"}.get(file.file_type, "📎")
    
    text = (
        f"{emoji} **{file.file_name}**\n\n"
        f"📦 Size: {format_size(file.file_size)}\n"
        f"🎭 Type: {file.file_type}\n"
    )
    
    if file.duration:
        text += f"⏱ Duration: {format_duration(file.duration)}\n"
    
    if file.public_hash:
        public_url = f"{settings.web_base_url}{build_public_stream_path(file)}"
        text += f"\n🔗 **Public Link:**\n`{public_url}`\n"
        share_btn = InlineKeyboardButton("🔗 Unshare", callback_data=f"unsharefile:{file.id}")
    else:
        share_btn = InlineKeyboardButton("🔗 Share", callback_data=f"sharefile:{file.id}")
    
    await message.reply(
        text,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✏️ Rename", callback_data=f"renamefile:{file.id}"),
                InlineKeyboardButton("📂 Move", callback_data=f"move:{file.id}"),
            ],
            [
                InlineKeyboardButton("🗑 Delete", callback_data=f"delfile:{file.id}"),
                share_btn,
            ],
        ])
    )


@tg_client.on_message(filters.command("deletefolder") & filters.private)
async def deletefolder_command(client, message: Message):
    """Delete a folder by name."""
    if len(message.command) < 2:
        await message.reply("Usage: /deletefolder <folder_name>")
        return
    
    folder_name = clean_bot_folder_name(" ".join(message.command[1:]))
    if not folder_name:
        await message.reply("❌ Invalid folder name.")
        return
    
    async with async_session() as db:
        user = await get_bot_user(db, message.from_user.id)
        if not user:
            await message.reply("Please use /start first.")
            return
        
        result = await db.execute(
            select(Folder).where(
                Folder.user_id == user.id,
                Folder.name == folder_name
            )
        )
        folder = result.scalar_one_or_none()
    
    if not folder:
        await message.reply(f"❌ Folder **{folder_name}** not found.")
        return
    
    # Show confirmation
    await message.reply(
        f"🗑 **Delete Folder?**\n\n"
        f"Folder: **{folder_name}**\n\n"
        "The folder and its contents will be recoverable from the web Recycle Bin while retention is enabled.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirmdelfolder:{folder.id}"),
                InlineKeyboardButton("❌ Cancel", callback_data="canceldel"),
            ]
        ])
    )
