FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Railway routes to 8080 by default and injects $PORT; run.py reads it in Python
# (no shell $PORT expansion needed).
ENV PORT=8080
EXPOSE 8080
CMD ["python", "run.py"]
