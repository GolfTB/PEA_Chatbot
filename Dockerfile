FROM python:3.11-slim

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps from repo-level requirements.txt
COPY mqtt/PEA_Chatbot/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy service code but DO NOT include host .env
COPY mqtt/PEA_Chatbot /app

ENV PYTHONUNBUFFERED=1

# Default command to run app_ev.py
CMD ["python", "app_ev.py"]
