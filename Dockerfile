# ── Roulette Signal Bot — Dockerfile ──────────────────────────────────────────
FROM python:3.13-slim

# Evita escritura de .pyc y asegura salida sin buffer
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Dependencias del sistema para matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfreetype6-dev \
    libpng-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Directorio de trabajo
WORKDIR /app

# Instala dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia TODO el contenido del repositorio al contenedor
COPY . .

# Render asigna automáticamente la variable $PORT
ENV PORT=10000
EXPOSE 10000

# Comando de inicio
CMD ["python", "main.py"]
