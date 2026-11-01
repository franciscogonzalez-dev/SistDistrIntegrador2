FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Usuario sin privilegios. La carpeta de datos se crea con este dueño antes de montar el volumen,
# asi Docker inicializa el volumen con los mismos permisos y el receptor puede escribir los avisos
RUN useradd --create-home receptor && mkdir -p /app/datos && chown receptor /app/datos
USER receptor

EXPOSE 5000

CMD ["python", "app.py"]
