FROM python:3.12-slim

WORKDIR /app

COPY servicio/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY comun/ comun/
COPY servicio/src/ servicio/src/

RUN useradd --create-home servicio && mkdir -p /app/servicio/data && chown servicio /app/servicio/data
USER servicio

CMD [ "python", "servicio/src/main.py"]