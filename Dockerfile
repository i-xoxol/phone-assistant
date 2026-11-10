# Source-only context; credentials and databases are runtime inputs.
FROM node:22-bookworm-slim AS codex
RUN npm install --global @openai/codex@0.163.0

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/data/runtime-home \
    DATABASE_PATH=/data/calls.sqlite3 OAUTH_DATABASE_PATH=/data/oauth.sqlite3 \
    APPOINTMENT_DATABASE_PATH=/data/appointments.sqlite3 \
    OWNER_PROMPT_DIRECTORY=/data/owner-prompts RECORDING_NOTICE_DIRECTORY=/data/recording-notices
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 phone && useradd --uid 10001 --gid phone --home-dir /data/runtime-home phone \
    && mkdir -p /data/runtime-home && chown -R phone:phone /data
COPY --chown=phone:phone app ./app
COPY --chown=phone:phone scripts ./scripts
USER phone

EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8766", "--workers", "1"]

# Opt-in managed PostgreSQL runtime. The default runtime stays SQLite-only.
FROM runtime AS managed
USER root
COPY requirements-managed.txt ./
RUN pip install --no-cache-dir -r requirements-managed.txt
USER phone

FROM managed AS managed-subscription
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
USER root
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
USER phone


# Optional subscription-backed lookups/review. Authentication is NOT in the image.
FROM runtime AS subscription
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
USER root
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
USER phone
