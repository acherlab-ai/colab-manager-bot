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
pip install -r requirements.txt   # python-telegram-bot, google-auth, git + jupyter-kernel-client
```

Sao chép `config.example.json` → `config.json` và điền token bot Telegram.

## Yêu cầu

- `colab` CLI (>=0.6.0) trong `PATH` (hoặc chỉ định `colab_bin` trong config).
- Các patch của bot cần có trong CLI:
  - `colab_cli/client.py::_issue_request` — `timeout` mặc định 600s (TPU provisioning > 120s).
  - `colab_cli/commands/session.py::stop` — unassign trước, đóng kernel sau (VM chết nhanh).

## Chạy

```bash
nohup python bot.py > bot_console.log 2>&1 &
```

## Lưu ý bảo mật

`config.json` và thư mục `data/` chứa secret (token Telegram, OAuth Google) và được `.gitignore` loại trừ — không push lên git.
