FROM python:3.13-slim

WORKDIR /app

# Install the package + deps first so this layer caches until pyproject changes.
# LICENSE and README.md are referenced by pyproject metadata, so hatchling needs
# them present at build time.
COPY pyproject.toml LICENSE README.md ./
COPY fireflyer ./fireflyer
RUN pip install --no-cache-dir -e ".[test,portal]"

# Sample data the default dashboard references (files/orders.csv).
COPY files ./files

EXPOSE 8000

# Bind 0.0.0.0 so the editor is reachable from the host. (The local
# `python -m fireflyer.web` entrypoint stays on 127.0.0.1.)
CMD ["uvicorn", "fireflyer.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
