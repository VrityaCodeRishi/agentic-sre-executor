FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install CA certs
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# Install dependencies (much faster now with OpenAI instead of sentence-transformers)
COPY agent/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copy your agent code
COPY agent/ /app/agent/

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "agent.service:app", "--host", "0.0.0.0", "--port", "8080"]