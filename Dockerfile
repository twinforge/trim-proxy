FROM alpine:3.19

RUN apk add --no-cache ffmpeg curl python3 py3-pip

WORKDIR /app

COPY server.py /app/server.py

EXPOSE 8080

CMD ["python3", "server.py"]
