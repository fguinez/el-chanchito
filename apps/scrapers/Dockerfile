FROM python:3.12-slim

# The build context is the repo root. requirements.txt installs the shared
# product-model package editable via `-e ../../packages/product-model`, so the
# package must sit at /repo/packages/ before pip runs and the app must live at
# /repo/apps/scrapers for the relative path to resolve.
WORKDIR /repo/apps/scrapers

COPY packages/product-model/ /repo/packages/product-model/
COPY apps/scrapers/requirements.txt .
# `--with-deps` pulls the apt libraries Chromium needs on the slim base; without
# it the BanChile scraper (one Playwright session for movements and balances)
# can't launch a browser and both legs no-op.
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium

COPY apps/scrapers/ .

CMD ["python", "main.py"]
