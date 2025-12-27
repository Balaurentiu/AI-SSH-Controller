# Folosim o imagine stabila de Python
FROM python:3.11

WORKDIR /app

# Instalam utilitarele de retea (ping)
RUN apt-get update && apt-get install -y iputils-ping && rm -rf /var/lib/apt/lists/*

# Cream un director dedicat pentru cheile SSH
RUN mkdir -p /app/keys

# Instalam pachetele Python
# Folosim setul de versiuni fixate care a rezolvat erorile
# 'ModuleNotFoundError' si 'DeadlineExceeded'.
RUN pip install --no-cache-dir \
    "pydantic>=2.0,<3.0" \
    "pydantic-settings" \
    "langchain==0.2.5" \
    "langchain-core==0.2.9" \
    "langchain-community==0.2.4" \
    "langchain-google-genai==1.0.5" \
    "langchain-anthropic>=0.1.0" \
    "google-generativeai" \
    "anthropic" \
    paramiko \
    Flask \
    Flask-SocketIO \
    requests \
    gunicorn \
    eventlet

# --- Copiem noile module refactorizate ---
COPY config.py .
COPY ssh_utils.py .
COPY llm_utils.py .
COPY log_manager.py .
COPY session_manager.py .
COPY agent_core.py .

# --- Copiem restul fisierelor aplicatiei ---
# Copiem app.py (fisierul principal)
COPY app.py .
# Copiem fisierul de configurare .ini
COPY config.ini .
# Copiem template-urile HTML
COPY templates ./templates

# Expunem portul intern
EXPOSE 5000

# --- CORECTIE: Adaugam --log-level=info si --capture-output ---
# Acest lucru va forta Gunicorn sa afiseze erorile (tracebacks) din aplicatia Python in 'docker logs'
CMD ["gunicorn", "--worker-class", "eventlet", "-w", "1", "--bind", "0.0.0.0:5000", "--timeout", "1200", "--log-level=info", "--capture-output", "app:app"]
