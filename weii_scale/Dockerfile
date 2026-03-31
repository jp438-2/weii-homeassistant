ARG BUILD_FROM
FROM $BUILD_FROM

# evdev requires these Alpine packages to compile its C extension
RUN apk add --no-cache \
    gcc \
    musl-dev \
    linux-headers \
    bluez

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY server.py .

CMD ["python3", "-u", "server.py"]
