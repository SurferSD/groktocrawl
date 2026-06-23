FROM python:3.13-slim
WORKDIR /app
COPY search-svc/pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY search-svc/search_svc/ search_svc/
COPY common/ common/
CMD ["uvicorn", "search_svc.app:app", "--host", "0.0.0.0", "--port", "8010"]
