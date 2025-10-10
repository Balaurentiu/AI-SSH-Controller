# Folosim o imagine stabila de Python
FROM python:3.11

WORKDIR /app

# Instalam utilitarele de retea (ping)
RUN apt-get update && apt-get install -y iputils-ping && rm -rf /var/lib/apt/lists/*

# Cream un director dedicat pentru cheile SSH in interiorul containerului
# Acesta va fi montat pe un volum persistent
RUN mkdir -p /app/keys

# MODIFICARE: Am scos openssh-client, vom folosi paramiko pentru generarea cheilor
# Instalam pachetele Python
RUN pip install --no-cache-dir langchain-community langchain-google-genai google-generativeai>=0.8.1 paramiko Flask Flask-SocketIO requests gunicorn eventlet

# Copiem TOATE fisierele necesare in container
COPY app.py .
COPY config.ini .
COPY templates ./templates

# Expunem portul
EXPOSE 5000

# Folosim Gunicorn pentru a porni aplicatia, cu un timeout mai mare
CMD ["gunicorn", "--worker-class", "eventlet", "-w", "1", "--bind", "0.0.0.0:5000", "--timeout", "300", "app:app"]

