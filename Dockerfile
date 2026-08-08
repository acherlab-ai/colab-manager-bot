FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Patch the installed google-colab-cli (keep-alive daemon, stop order,
# request timeout). Idempotent, safe to re-run at boot if needed.
RUN python apply_patches.py

ENV DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8443

CMD ["python", "bot.py"]
