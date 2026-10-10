FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dmiot2mqtt.py dmiot2mqtt.ini ./
RUN useradd --system --no-create-home dmiot2mqtt
USER dmiot2mqtt

EXPOSE 31270
# Mount your dmiot2mqtt.ini at /config/dmiot2mqtt.ini
ENTRYPOINT ["python3", "-u", "/app/dmiot2mqtt.py"]
CMD ["-c", "/config/dmiot2mqtt.ini"]
