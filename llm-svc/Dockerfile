FROM python:3.13-slim
WORKDIR /app
COPY llm-svc/pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY llm-svc/llm_svc/ llm_svc/
COPY common/ common/
CMD ["uvicorn", "llm_svc.app:app", "--host", "0.0.0.0", "--port", "8011"]
