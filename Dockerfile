FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

VOLUME /assets
EXPOSE 5000

# One worker on purpose: the scan, dashboard and Plex/Assets caches live in this
# process's memory (several workers would each keep their own and overwrite the
# state file). Concurrency comes from the threads.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "300", \
     "--worker-tmp-dir", "/dev/shm", "--access-logfile", "-", \
     "--bind", "0.0.0.0:5000", "app:app"]
