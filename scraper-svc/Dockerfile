FROM python:3.13-slim

WORKDIR /app
COPY scraper-svc/pyproject.toml .
RUN pip install --no-cache-dir -e .

# Install with playwright extras for Tier 3
RUN pip install --no-cache-dir -e ".[playwright]"
RUN python -m playwright install chromium
RUN python -m playwright install-deps chromium

COPY scraper-svc/scraper/ scraper/
COPY common/ common/

EXPOSE 8001
CMD ["uvicorn", "scraper.app:app", "--host", "0.0.0.0", "--port", "8001"]
