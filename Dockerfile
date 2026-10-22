FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
RUN python app/prefetch.py
ENV PYTHONUNBUFFERED=1
CMD exec uvicorn server:app --app-dir app --host 0.0.0.0 --port ${PORT:-8080}
