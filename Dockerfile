FROM alpine:3.24

# Use Alpine packages: PyPI's OpenCV wheels require glibc, not musl.
RUN apk add --no-cache python3 py3-numpy py3-opencv ffmpeg tzdata \
    && addgroup -S -g 10001 timelapse \
    && adduser -S -D -H -u 10001 -G timelapse timelapse \
    && mkdir -p /app /data \
    && chown timelapse:timelapse /data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

COPY src/tlapse/ /app/src/tlapse/

WORKDIR /data
USER timelapse

# The application handles SIGTERM and flushes pending video frames.
STOPSIGNAL SIGTERM
ENTRYPOINT ["python3", "-m", "tlapse"]
CMD []
