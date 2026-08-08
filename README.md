# colab-manager-bot

Telegram bot quản lý Google Colab labs: tạo lab CPU / GPU / TPU kèm link sshx, theo dõi và dừng lab từ Telegram bằng reply keyboard.

## Tính năng

- Đăng ký tối đa 3 tài khoản Google cho mỗi user (OAuth2 device flow).
- Tạo lab CPU / GPU / TPU với các loại cấu hình (`config.json`).
- Tự động chạy **sshx** trên lab và trả link truy cập terminal.
- Keep-alive lab tối đa ~24h (daemon do `colab` CLI sinh ra).
- Check lab, dừng lab. Khi dừng, bot giết toàn bộ process trên VM trước khi `colab stop` để link sshx chết ngay.

## Cài đặt

```bash
pip install -r requirements.txt   # python-telegram-bot, google-auth-oauthlib, google-colab-cli, requests
python apply_patches.py           # tái lập 2 patch cần thiết cho google-colab-cli
```

Sao chép `config.example.json` → `config.json` và điền token bot Telegram (hoặc dùng env var `BOT_TOKEN`).

## Yêu cầu

- `colab` CLI (`google-colab-cli` >=0.6.0) — tự cài qua `requirements.txt`.
- 2 patch cần thiết (bot tự áp qua `apply_patches.py`):
  - `colab_cli/client.py` — `timeout` mặc định 600s (TPU provisioning > 120s).
  - `colab_cli/commands/session.py::stop` — unassign trước, đóng kernel sau (VM chết nhanh).

## Chạy

```bash
python apply_patches.py
nohup python bot.py > bot_console.log 2>&1 &
```

## Deploy lên Railway

`railway.toml` + `apply_patches.py` đã sẵn trong repo. Bot đọc cấu hình từ **env var** (không cần `config.json`):

| Env var | Mô tả |
|---|---|
| `BOT_TOKEN` | Token Telegram bot (bắt buộc) |
| `DATA_DIR` | Thư mục dữ liệu (mặc định `./data`) |
| `MAX_ACCOUNTS_PER_USER` | Giới hạn tài khoản mỗi user (mặc định 3) |
| `MAX_HANG_HOURS` | Thời gian lab tối đa (mặc định 24) |
| `COLAB_BIN` | Đường dẫn `colab` CLI (tự dò PATH nếu bỏ trống) |

- `startCommand = "python apply_patches.py && python bot.py"` — `apply_patches.py` tự tái lập 2 patch cho `google-colab-cli` (timeout 600s + unassign-trước-khi-stop).
- Vì filesystem Railway là tạm thời, `data/` (OAuth Google + session) sẽ mất khi redeploy — cần đăng nhập lại qua `/login`.

## Lưu ý bảo mật

`config.json` và thư mục `data/` chứa secret (token Telegram, OAuth Google) và được `.gitignore` loại trừ — không push lên git.
