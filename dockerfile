# Folosim o imagine stabila de Python
FROM python:3.11

WORKDIR /app

# Set timezone to Romania/Bucharest
ENV TZ=Europe/Bucharest
RUN apt-get update && apt-get install -y \
    iputils-ping \
    tzdata \
    tesseract-ocr \
    poppler-utils \
    libcairo2-dev \
    libpango1.0-dev \
    libgdk-pixbuf-xlib-2.0-dev \
    libffi-dev \
    shared-mime-info \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

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
    simple-websocket \
    PyPDF2 \
    python-docx \
    chromadb \
    faiss-cpu \
    langchain-text-splitters \
    numpy \
    pytesseract \
    pdf2image \
    Pillow \
    ddgs \
    beautifulsoup4 \
    trafilatura \
    markdown \
    xhtml2pdf \
    cairosvg \
    matplotlib \
    pyTelegramBotAPI

# --- Copiem noile module refactorizate ---
COPY config.py .
COPY ssh_utils.py .
COPY llm_utils.py .
COPY log_manager.py .
COPY session_manager.py .
COPY agent_core.py .
COPY knowledge_manager.py .
COPY web_search_module.py .
COPY chat_export.py .
COPY telegram_bot.py .

# --- Copiem restul fisierelor aplicatiei ---
# Copiem app.py (fisierul principal)
COPY app.py .
# Copiem fisierul de configurare .ini
COPY config.ini .
# Copiem template-urile HTML
COPY templates ./templates

# Expunem portul intern
EXPOSE 5000

# Launch directly with socketio.run() in threading mode
# Gunicorn with eventlet blocked the entire event loop on slow LLM calls
CMD ["python3", "app.py"]
