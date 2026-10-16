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

CMD ["flask", "run", "--host=0.0.0.0"]
