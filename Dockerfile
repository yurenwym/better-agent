FROM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.12-slim AS app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 BETTER_AGENT_HOST=0.0.0.0 PORT=8000
WORKDIR /app
COPY backend ./backend
RUN pip install --no-cache-dir ./backend
COPY scripts ./scripts
COPY README.md LICENSE ./
COPY --from=frontend /src/frontend/dist ./frontend/dist
RUN mkdir -p /app/data
VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"
CMD ["python", "scripts/serve.py"]
