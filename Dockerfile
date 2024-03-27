FROM python:3.10

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget \
        curl && \
    rm -rf /var/lib/apt/lists/*


WORKDIR /app
COPY . .
RUN pip install --no-cache-dir --upgrade pip && pip install -r requirements-optional.txt

CMD ["python", "app.py"]
