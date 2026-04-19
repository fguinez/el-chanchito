FROM python:3.12-slim

WORKDIR /app

COPY apps/scrapers/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/scrapers/ .

CMD ["python", "main.py"]
