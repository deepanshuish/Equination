FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    EQUINATION_HOST=0.0.0.0 \
    EQUINATION_DATA_DIR=/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "run.py"]
