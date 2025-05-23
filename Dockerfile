FROM python:3.11-alpine

WORKDIR /app
ADD https://raw.githubusercontent.com/vishnubob/wait-for-it/master/wait-for-it.sh /usr/bin/wait-for-it

RUN chmod +x /usr/bin/wait-for-it

RUN apk add --no-cache \
    postgresql-dev \
    gcc \
    python3-dev \
    musl-dev

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "while ! nc -z tracker_db 5432; do sleep 1; done && python bot.py"]
