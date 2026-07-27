FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_DB_PATH=/var/lib/claude-monitor/state.db

WORKDIR /app
RUN groupadd --system monitor && useradd --system --gid monitor monitor \
    && mkdir -p /var/lib/claude-monitor && chown monitor:monitor /var/lib/claude-monitor
COPY pyproject.toml requirements.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps . \
    && chown -R monitor:monitor /app

USER monitor
EXPOSE 8080
CMD ["uvicorn", "claude_monitor.service:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
