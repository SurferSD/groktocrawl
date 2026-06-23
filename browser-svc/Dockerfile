FROM python:3.13-slim

WORKDIR /app
COPY browser-svc/pyproject.toml .
RUN pip install --no-cache-dir -e .
RUN python -m playwright install chromium
RUN python -m playwright install-deps chromium

COPY browser-svc/browser_svc/ browser_svc/
COPY common/ common/

EXPOSE 8012
CMD ["uvicorn", "browser_svc.app:app", "--host", "0.0.0.0", "--port", "8012"]
