FROM python:3.12-slim

# git: the demo services are git repositories, and rollback resets them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# AGENT_USER is the name written to the audit log (no login in version 1).
ENV PORT=8080 \
    AGENT_USER=demo-user \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["python", "app.py"]
