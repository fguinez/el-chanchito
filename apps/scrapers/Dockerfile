FROM python:3.12-slim

WORKDIR /app

COPY apps/scrapers/requirements.txt .
# `--with-deps` pulls the apt libraries Chromium needs on the slim base; without
# it the BanChile scraper (fintself transactions + banchile_web balances) can't
# launch a browser and both legs no-op.
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium

COPY apps/scrapers/ .

CMD ["python", "main.py"]
