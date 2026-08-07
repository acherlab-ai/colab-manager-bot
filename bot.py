import asyncio
import json
import logging
import os
import re
from datetime import datetime

from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import colab as colab_ops
import oauth as oauth_ops
from store import UserStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_config() -> dict:
    cfg = {}
    try:
        with open(os.environ.get("BOT_CONFIG", os.path.join(BASE_DIR, "config.json"))) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        pass
    return {
        "bot_token": os.environ.get("BOT_TOKEN", cfg.get("bot_token", "")),
        "data_dir": os.environ.get("DATA_DIR", cfg.get("data_dir", os.path.join(BASE_DIR, "data"))),
        "max_accounts_per_user": int(
            os.environ.get("MAX_ACCOUNTS_PER_USER", cfg.get("max_accounts_per_user", 3))
        ),
        "gpu_types": cfg.get("gpu_types", ["T4", "L4", "G4", "H100", "A100"]),
        "tpu_types": cfg.get("tpu_types", ["v5e1", "v6e1"]),
        "max_hang_hours": int(os.environ.get("MAX_HANG_HOURS", cfg.get("max_hang_hours", 24))),
    }


CONFIG = _load_config()
DATA_DIR = CONFIG["data_dir"]
MAX_ACCOUNTS = CONFIG["max_accounts_per_user"]
MAX_HANG_HOURS = CONFIG["max_hang_hours"]

try:
    import jupyter_kernel_client

    _JKC_VER = getattr(jupyter_kernel_client, "__version__", "?")
    _JKC_HAS_KC = hasattr(jupyter_kernel_client, "KernelClient")
except Exception as _e:  # noqa: BLE001
    _JKC_VER = f"import failed: {_e}"
    _JKC_HAS_KC = False

store = UserStore(DATA_DIR)

pending_auth: dict[int, str] = {}
user_state: dict[int, dict] = {}
user_locks: dict[int, asyncio.Lock] = {}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    filename=os.environ.get("LOG_PATH", os.path.join(BASE_DIR, "bot.log")),
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)


def log(msg: str):
    logging.info(msg)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


log(f"jupyter_kernel_client version={_JKC_VER} has KernelClient={_JKC_HAS_KC}")


# ---- keyboard builders ----

MENU = "↩️ MENU"
BTN_LOGIN = "🔑 ĐĂNG NHẬP"
BTN_DEL = "🗑 XOÁ ĐĂNG NHẬP"
BTN_CHECKACC = "👤 CHECK TÀI KHOẢN"
BTN_CREATE = "🖥 TẠO LABS"
BTN_CHECKLABS = "📋 CHECK LABS"
BTN_STOPLABS = "⏹ STOP LABS"
BTN_CPU = "🖥 CPU"
BTN_GPU = "🎮 GPU"
BTN_TPU = "🔮 TPU"


def rkb(rows: list) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def main_keyboard() -> ReplyKeyboardMarkup:
    return rkb(
        [
            [BTN_LOGIN, BTN_DEL],
            [BTN_CHECKACC],
            [BTN_CREATE, BTN_CHECKLABS],
            [BTN_STOPLABS],
        ]
    )


def with_back(rows: list) -> ReplyKeyboardMarkup:
    return rkb(rows + [[MENU]])


def accounts_keyboard(chat_id: int) -> ReplyKeyboardMarkup | None:
    accs = store.list_accounts(chat_id)
    if not accs:
        return None
    return with_back([[a["name"]] for a in accs])


# ---- helpers ----

async def reply(update: Update, text: str, keyboard=None):
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


def reset(uid: int):
    user_state[uid] = {"flow": "main"}


MAIN_BUTTONS = {BTN_LOGIN, BTN_DEL, BTN_CHECKACC, BTN_CREATE, BTN_CHECKLABS, BTN_STOPLABS}


def user_busy(uid: int) -> bool:
    lock = user_locks.get(uid)
    return lock is not None and lock.locked()


def fmt_accounts(uid: int) -> str:
    accs = store.list_accounts(uid)
    if not accs:
        return ""
    return "\n".join(f"⚙️ <b>{a['name']}</b> — {a['email']}" for a in accs)


LAB_RE = re.compile(
    r"\[(?P<name>[^\]]+)\]\s+\S+\s*\|\s*Hardware:\s*(?P<hardware>[^|]*?)\s*"
    r"\|\s*Variant:\s*(?P<variant>[^|]*?)(?:\s*\|\s*Status:\s*(?P<status>.*))?$"
)


def parse_labs(output: str) -> list[dict]:
    labs = []
    for line in output.splitlines():
        m = LAB_RE.search(line)
        if m:
            labs.append(
                {
                    "name": m.group("name").strip(),
                    "hardware": m.group("hardware").strip(),
                    "variant": m.group("variant").strip(),
                    "status": (m.group("status") or "").strip() or None,
                }
            )
    return labs


# ---- handlers ----

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_chat.id
    first = store.register_user(uid)
    reset(uid)
    log(f"/start from {uid} (new={first})")
    await reply(
        update,
        "🤖 <b>Colab Manager Bot</b>\n"
        "Quản lý labs Google Colab từ Telegram.\n\n"
        "• Tối đa <b>3 tài khoản Google</b> cho mỗi user\n"
        "• Tạo labs CPU / GPU / TPU kèm link <b>sshx</b>\n"
        "• Thời gian treo tối đa mỗi lab: <b>~24h</b>\n\n"
        f"<code>/ver</code> để kiểm tra phiên bản đang chạy\n"
        "Chọn chức năng dưới bàn phím 👇",
        main_keyboard(),
    )


async def ver(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_chat.id
    ver = "?"
    try:
        with open(os.path.join(BASE_DIR, "VERSION")) as f:
            ver = f.read().strip()
    except Exception:
        pass
    procs = len(colab_ops._KEEPALIVE_PROCS)
    await reply(
        update,
        f"🛠 <b>Phiên bản</b>: <code>{ver}</code>\n"
        f"🔄 Keep-alive daemon đang chạy: <b>{procs}</b>\n"
        f"👤 User: <code>{uid}</code>",
        main_keyboard(),
    )


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_chat.id
    text = (update.message.text or "").strip()

    if text.startswith("4/") and uid in pending_auth:
        await handle_auth_code(update, text)
        return

    if text == MENU:
        reset(uid)
        await reply(update, "🎛 <b>Menu chính</b> — chọn chức năng:", main_keyboard())
        return

    st = user_state.get(uid, {"flow": "main"})
    flow = st.get("flow", "main")

    if text in MAIN_BUTTONS:
        reset(uid)
        if user_busy(uid):
            await reply(update, "⏳ Đang xử lý thao tác trước, xin chờ...")
            return
        await handle_main(update, text)
        return

    if user_busy(uid):
        await reply(update, "⏳ Đang xử lý thao tác trước, xin chờ...")
        return

    if flow == "main":
        await handle_main(update, text)
    elif flow == "del_account":
        await handle_del_account(update, st, text)
    elif flow == "create_account":
        await handle_create_account(update, st, text)
    elif flow == "create_type":
        await handle_create_type(update, st, text)
    elif flow == "create_gpu":
        await handle_create_gpu(update, st, text)
    elif flow == "create_tpu":
        await handle_create_tpu(update, st, text)
    elif flow == "check_account":
        await handle_check_account(update, st, text)
    elif flow == "stop_account":
        await handle_stop_account(update, st, text)
    elif flow == "stop_lab":
        await handle_stop_lab(update, st, text)
    else:
        reset(uid)
        await reply(update, "🎛 Menu chính:", main_keyboard())


async def handle_main(update: Update, text: str):
    uid = update.effective_chat.id
    if text == BTN_LOGIN:
        await start_login(update)
    elif text == BTN_DEL:
        await show_del_accounts(update)
    elif text == BTN_CHECKACC:
        await show_check_accounts(update)
    elif text == BTN_CREATE:
        await show_create_accounts(update)
    elif text == BTN_CHECKLABS:
        await show_check_labs(update)
    elif text == BTN_STOPLABS:
        await show_stop_labs(update)
    else:
        await reply(update, "Chọn chức năng dưới bàn phím 👇", main_keyboard())


# ---- login ----

async def start_login(update: Update):
    uid = update.effective_chat.id
    accs = store.list_accounts(uid)
    if len(accs) >= MAX_ACCOUNTS:
        await reply(
            update,
            f"❌ Bạn đã đủ <b>{MAX_ACCOUNTS} tài khoản</b>.\n"
            "Bấm <b>XOÁ ĐĂNG NHẬP</b> để xoá bớt trước khi đăng nhập mới.",
            main_keyboard(),
        )
        return
    acc_name = store.next_account_name(uid, MAX_ACCOUNTS)
    home = store.account_home(uid, acc_name)
    try:
        url = await asyncio.to_thread(oauth_ops.generate_auth_url, home)
    except Exception as e:
        log(f"login gen url err {uid}: {e}")
        await reply(update, f"❌ Lỗi tạo URL xác thực: {e}", main_keyboard())
        return
    pending_auth[uid] = acc_name
    log(f"login requested acc={acc_name} for {uid}")
    await reply(
        update,
        f"🔑 <b>ĐĂNG NHẬP TÀI KHOẢN {acc_name.upper()}</b>\n\n"
        f"1️⃣ Truy cập link (đăng nhập Google):\n<code>{url}</code>\n\n"
        f"2️⃣ Google hiển thị mã <b>4/...</b> → copy và gửi vào chat.\n\n"
        f"⏳ Mỗi lần bấm ĐĂNG NHẬP sinh mã mới.",
    )


async def handle_auth_code(update: Update, code: str):
    uid = update.effective_chat.id
    acc_name = pending_auth.pop(uid)
    home = store.account_home(uid, acc_name)
    await reply(update, f"🔄 Đang xác thực tài khoản {acc_name.upper()}...")
    try:
        email = await asyncio.to_thread(oauth_ops.exchange_code, home, code)
    except Exception as e:
        log(f"auth exchange err {uid} {acc_name}: {e}")
        await reply(
            update,
            f"❌ Xác thực thất bại: <code>{e}</code>\n\n"
            "Bấm <b>ĐĂNG NHẬP</b> lại để có mã mới.",
            main_keyboard(),
        )
        return
    store.add_account(uid, acc_name, email)
    reset(uid)
    log(f"account added {uid} {acc_name} {email}")
    await reply(
        update,
        f"✅ <b>ĐĂNG NHẬP THÀNH CÔNG</b>\n\n"
        f"⚙️ Tài khoản: <b>{acc_name.upper()}</b>\n"
        f"📧 Email: <b>{email}</b>\n"
        f"📊 Đã dùng: {len(store.list_accounts(uid))}/{MAX_ACCOUNTS} tài khoản",
        main_keyboard(),
    )


# ---- delete account ----

async def show_del_accounts(update: Update):
    uid = update.effective_chat.id
    accs = store.list_accounts(uid)
    if not accs:
        await reply(update, "❌ Chưa có tài khoản nào để xoá.", main_keyboard())
        return
    user_state[uid] = {"flow": "del_account"}
    kb = accounts_keyboard(uid)
    await reply(
        update,
        f"🗑 <b>XOÁ ĐĂNG NHẬP</b>\nChọn tài khoản cần xoá:\n\n{fmt_accounts(uid)}",
        kb,
    )


async def handle_del_account(update: Update, st: dict, text: str):
    uid = update.effective_chat.id
    if not store.has_account(uid, text):
        await reply(update, "❌ Không thấy tài khoản đó.", main_keyboard())
        reset(uid)
        return
    acc_name = text
    await reply(update, f"🗑 Đang xoá tài khoản {acc_name.upper()}...")
    stopped = []
    try:
        out = await asyncio.to_thread(
            colab_ops.list_labs, store.account_home(uid, acc_name)
        )
        for lab in parse_labs(out):
            try:
                await asyncio.to_thread(
                    colab_ops.stop_lab, store.account_home(uid, acc_name), lab["name"]
                )
                stopped.append(lab["name"])
            except Exception as e:
                log(f"stop {acc_name}/{lab['name']} err: {e}")
    except Exception as e:
        log(f"delaccount lab scan err {uid}: {e}")
    store.remove_account(uid, acc_name)
    reset(uid)
    log(f"account removed {uid} {acc_name}")
    msg = f"✅ Đã xoá tài khoản <b>{acc_name.upper()}</b>."
    if stopped:
        msg += "\n⏹ Đã dừng labs: " + ", ".join(f"<code>{s}</code>" for s in stopped)
    msg += f"\n\nCòn lại: {len(store.list_accounts(uid))}/{MAX_ACCOUNTS} tài khoản"
    await reply(update, msg, main_keyboard())


# ---- check accounts ----

async def show_check_accounts(update: Update):
    uid = update.effective_chat.id
    accs = store.list_accounts(uid)
    if not accs:
        await reply(update, "❌ Chưa có tài khoản nào.\nBấm <b>ĐĂNG NHẬP</b> để thêm.", main_keyboard())
        return
    lines = ["👤 <b>CHECK TÀI KHOẢN</b>\n"]
    for a in accs:
        home = store.account_home(uid, a["name"])
        n_labs = "?"
        try:
            out = await asyncio.to_thread(colab_ops.list_labs, home)
            n_labs = len(parse_labs(out))
        except Exception as e:
            log(f"checkacc labs err {uid} {a['name']}: {e}")
        lines.append(
            f"⚙️ <b>{a['name'].upper()}</b>\n"
            f"   📧 {a['email']}\n"
            f"   🖥 Labs đang chạy: <b>{n_labs}</b>\n"
        )
    lines.append(f"📊 Đã dùng: <b>{len(accs)}/{MAX_ACCOUNTS}</b> tài khoản")
    await reply(update, "\n".join(lines), main_keyboard())


# ---- create labs ----

async def show_create_accounts(update: Update):
    uid = update.effective_chat.id
    kb = accounts_keyboard(uid)
    if kb is None:
        await reply(
            update,
            "❌ Bạn chưa đăng nhập tài khoản Google nào.\nBấm <b>ĐĂNG NHẬP</b> trước.",
            main_keyboard(),
        )
        return
    user_state[uid] = {"flow": "create_account"}
    await reply(
        update,
        f"🖥 <b>TẠO LABS</b>\nChọn tài khoản:\n\n{fmt_accounts(uid)}",
        kb,
    )


async def handle_create_account(update: Update, st: dict, text: str):
    uid = update.effective_chat.id
    if not store.has_account(uid, text):
        await reply(update, "❌ Không thấy tài khoản đó.", main_keyboard())
        reset(uid)
        return
    user_state[uid] = {"flow": "create_type", "acc": text}
    await reply(
        update,
        f"⚙️ <b>{text.upper()}</b> — chọn loại lab:",
        with_back([[BTN_CPU], [BTN_GPU], [BTN_TPU]]),
    )


async def handle_create_type(update: Update, st: dict, text: str):
    uid = update.effective_chat.id
    acc = st["acc"]
    if text == BTN_CPU:
        await do_create(update, acc, gpu=None, tpu=None)
    elif text == BTN_GPU:
        user_state[uid] = {"flow": "create_gpu", "acc": acc}
        await reply(
            update,
            f"🎮 <b>GPU</b> — chọn loại GPU:",
            with_back([[g] for g in CONFIG["gpu_types"]]),
        )
    elif text == BTN_TPU:
        user_state[uid] = {"flow": "create_tpu", "acc": acc}
        await reply(
            update,
            f"🔮 <b>TPU</b> — chọn loại TPU:",
            with_back([[t] for t in CONFIG["tpu_types"]]),
        )
    else:
        await reply(update, "❌ Chọn CPU / GPU / TPU bên dưới.", with_back([[BTN_CPU], [BTN_GPU], [BTN_TPU]]))


async def handle_create_gpu(update: Update, st: dict, text: str):
    if text in CONFIG["gpu_types"]:
        await do_create(update, st["acc"], gpu=text, tpu=None)
    else:
        await reply(update, "❌ Chọn loại GPU bên dưới.", with_back([[g] for g in CONFIG["gpu_types"]]))


async def handle_create_tpu(update: Update, st: dict, text: str):
    if text in CONFIG["tpu_types"]:
        await do_create(update, st["acc"], gpu=None, tpu=text)
    else:
        await reply(update, "❌ Chọn loại TPU bên dưới.", with_back([[t] for t in CONFIG["tpu_types"]]))


async def do_create(update: Update, acc_name: str, gpu=None, tpu=None):
    uid = update.effective_chat.id
    lock = user_locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await reply(update, "⏳ Đang có thao tác khác, xin chờ...")
        return
    async with lock:
        home = store.account_home(uid, acc_name)
        hw_label = gpu or tpu or "CPU"
        await reply(update, f"⏳ Đang tạo lab <b>{hw_label}</b> trên <b>{acc_name.upper()}</b>...")
        name = colab_ops.new_name()
        try:
            await asyncio.to_thread(colab_ops.create_lab, home, name, gpu, tpu)
        except Exception as e:
            log(f"create lab err {uid} {acc_name}: {e}")
            reset(uid)
            await reply(
                update,
                f"❌ <b>TẠO LAB THẤT BẠI</b>\n\n<code>{e}</code>\n\n<i>Thử loại hardware khác hoặc CPU.</i>",
                main_keyboard(),
            )
            return
        log(f"lab created {uid} {acc_name} {name} {hw_label}")
        await reply(
            update,
            f"✅ <b>LAB ĐÃ TẠO</b>\n\n"
            f"🆔 ID: <code>{name}</code>\n"
            f"⚙️ Tài khoản: {acc_name.upper()}\n"
            f"🖥 Hardware: <b>{hw_label}</b>\n"
            f"⏳ Thời gian treo tối đa: <b>~{MAX_HANG_HOURS}h</b>\n\n"
            f"🔗 Đang khởi động sshx, xin chờ...",
        )
        try:
            link = await asyncio.to_thread(colab_ops.start_sshx, home, name)
        except Exception as e:
            log(f"sshx err {uid} {name}: {e}")
            reset(uid)
            await reply(
                update,
                f"✅ <b>LAB ĐÃ TẠO</b>\n\n"
                f"🆔 ID: <code>{name}</code>\n"
                f"⚙️ Tài khoản: {acc_name.upper()}\n"
                f"🖥 Hardware: <b>{hw_label}</b>\n"
                f"⏳ Thời gian treo tối đa: <b>~{MAX_HANG_HOURS}h</b>\n\n"
                f"❌ Lỗi sshx: <code>{e}</code>",
                main_keyboard(),
            )
            return
        reset(uid)
        log(f"sshx ready {uid} {name} {link}")
        await reply(
            update,
            f"✅ <b>LAB SẴN SÀNG</b>\n\n"
            f"🆔 ID: <code>{name}</code>\n"
            f"⚙️ Tài khoản: {acc_name.upper()}\n"
            f"🖥 Hardware: <b>{hw_label}</b>\n"
            f"⏳ Thời gian treo tối đa: <b>~{MAX_HANG_HOURS}h</b>\n"
            f"💾 Data không bền — lưu vào Drive khi cần\n\n"
            f"🔗 <b>Link sshx:</b>\n<code>{link}</code>\n\n"
            f"🛑 Dừng lab: bấm <b>STOP LABS</b>",
            main_keyboard(),
        )


# ---- check labs ----

async def show_check_labs(update: Update):
    uid = update.effective_chat.id
    kb = accounts_keyboard(uid)
    if kb is None:
        await reply(update, "❌ Bạn chưa đăng nhập tài khoản nào.", main_keyboard())
        return
    user_state[uid] = {"flow": "check_account"}
    await reply(
        update,
        f"📋 <b>CHECK LABS</b>\nChọn tài khoản:\n\n{fmt_accounts(uid)}",
        kb,
    )


async def handle_check_account(update: Update, st: dict, text: str):
    uid = update.effective_chat.id
    if not store.has_account(uid, text):
        await reply(update, "❌ Không thấy tài khoản đó.", main_keyboard())
        reset(uid)
        return
    acc_name = text
    home = store.account_home(uid, acc_name)
    await reply(update, f"🔄 Đang kiểm tra labs {acc_name.upper()}...")
    try:
        out = await asyncio.to_thread(colab_ops.list_labs, home)
    except Exception as e:
        log(f"checklabs err {uid}: {e}")
        reset(uid)
        await reply(update, f"❌ Lỗi: <code>{e}</code>", main_keyboard())
        return
    labs = parse_labs(out)
    reset(uid)
    if not labs:
        await reply(update, f"📭 <b>{acc_name.upper()}</b>: không có lab đang chạy.", main_keyboard())
        return
    lines = [f"📋 <b>CHECK LABS — {acc_name.upper()}</b>\n"]
    for l in labs:
        lines.append(
            f"🆔 <code>{l['name']}</code>\n"
            f"   🖥 Hardware: <b>{l['hardware']}</b>\n"
            f"   🏷 Variant: {l['variant']}"
            + (f"\n   📶 Status: {l['status']}" if l.get("status") else "")
            + "\n"
        )
    await reply(update, "\n".join(lines), main_keyboard())


# ---- stop labs ----

async def show_stop_labs(update: Update):
    uid = update.effective_chat.id
    kb = accounts_keyboard(uid)
    if kb is None:
        await reply(update, "❌ Bạn chưa đăng nhập tài khoản nào.", main_keyboard())
        return
    user_state[uid] = {"flow": "stop_account"}
    await reply(
        update,
        f"⏹ <b>STOP LABS</b>\nChọn tài khoản:\n\n{fmt_accounts(uid)}",
        kb,
    )


async def handle_stop_account(update: Update, st: dict, text: str):
    uid = update.effective_chat.id
    if not store.has_account(uid, text):
        await reply(update, "❌ Không thấy tài khoản đó.", main_keyboard())
        reset(uid)
        return
    acc_name = text
    home = store.account_home(uid, acc_name)
    try:
        out = await asyncio.to_thread(colab_ops.list_labs, home)
    except Exception as e:
        log(f"stopsel err {uid}: {e}")
        reset(uid)
        await reply(update, f"❌ Lỗi: <code>{e}</code>", main_keyboard())
        return
    labs = parse_labs(out)
    if not labs:
        reset(uid)
        await reply(update, f"📭 <b>{acc_name.upper()}</b>: không có lab để dừng.", main_keyboard())
        return
    user_state[uid] = {"flow": "stop_lab", "acc": acc_name}
    lines = "\n".join(f"🆔 <code>{l['name']}</code> — {l['hardware']}" for l in labs)
    await reply(
        update,
        f"⏹ <b>STOP LABS — {acc_name.upper()}</b>\nBấm đúng ID lab để dừng:\n\n{lines}",
        with_back([[l["name"]] for l in labs]),
    )


async def handle_stop_lab(update: Update, st: dict, text: str):
    uid = update.effective_chat.id
    acc_name = st["acc"]
    home = store.account_home(uid, acc_name)
    try:
        out = await asyncio.to_thread(colab_ops.list_labs, home)
    except Exception:
        out = ""
    if text not in {l["name"] for l in parse_labs(out)}:
        reset(uid)
        await reply(update, "❌ Không thấy lab đó. Bấm <b>STOP LABS</b> lại.", main_keyboard())
        return
    await reply(update, f"⏹ Đang dừng lab <code>{text}</code>...")
    try:
        out = await asyncio.to_thread(colab_ops.stop_lab, home, text)
    except Exception as e:
        log(f"stop err {uid} {text}: {e}")
        reset(uid)
        await reply(update, f"❌ Dừng lab thất bại:\n<code>{e}</code>", main_keyboard())
        return
    reset(uid)
    log(f"lab stopped {uid} {acc_name} {text}")
    await reply(
        update,
        f"✅ Đã dừng lab <code>{text}</code> và giải phóng VM.\n<code>{out.strip()[-200:]}</code>",
        main_keyboard(),
    )


def _scan_active_sessions() -> list[tuple[str, str, str]]:
    """Return (account_home, session_name, endpoint) for every live session."""
    found: list[tuple[str, str, str]] = []
    for chat_id in list(getattr(store, "_data", {}).keys()):
        for acc in store.list_accounts(chat_id):
            home = store.account_home(chat_id, acc["name"])
            cfg = os.path.join(home, ".config", "colab-cli", "sessions.json")
            try:
                with open(cfg) as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            for name, s in data.items():
                if isinstance(s, dict) and s.get("endpoint"):
                    found.append((home, name, s["endpoint"]))
    return found


async def keepalive_watchdog():
    """Respawn any dead `colab keep-alive` daemons so labs don't get
    idle-pruned after ~15 min when the detached CLI daemon dies."""
    while True:
        try:
            sessions = _scan_active_sessions()
            if sessions:
                log(f"keepalive watch: {len(sessions)} active session(s)")
            await asyncio.to_thread(colab_ops.reconcile_keepalives, sessions)
        except Exception as e:
            log(f"keepalive watchdog error: {e}")
        await asyncio.sleep(60)


def main():
    async def post_init(_app):
        _app.create_task(keepalive_watchdog())
        log("keepalive watchdog started")

    app = Application.builder().token(CONFIG["bot_token"]).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ver", ver))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
