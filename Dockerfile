FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY Dataset/ ./Dataset/
RUN mkdir -p var

# Parse the document pack at BUILD time. A dataset that cannot be read should
# break the build, not the first customer question in production.
# NOTE: Embedding index is built at runtime (requires JINA API key).
RUN python -c "\
from app import store; \
c = store.load_workbook(); \
print(f'[build] records={c}')"

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)"

# One worker: the vector index and the parsed rules are in-process, and the
# sessions are in memory. Scaling out means moving sessions to Redis first.
CMD ["sh","-c","uvicorn app.api:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
