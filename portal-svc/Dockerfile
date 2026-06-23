FROM python:3.13-slim

WORKDIR /app
COPY portal-svc/pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY portal-svc/portal/ portal/
COPY common/ common/

CMD ["uvicorn", "portal.app:app", "--host", "0.0.0.0", "--port", "8081"]
