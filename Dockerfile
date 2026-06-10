FROM python:3.12-slim

WORKDIR /app

# 의존성 먼저(레이어 캐시)
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# 백엔드 + 기존 프론트(index.html) 동시 서빙 (단일 컨테이너)
COPY backend/ backend/
COPY index.html index.html

WORKDIR /app/backend
ENV PORT=8080
EXPOSE 8080

# main.py 의 ROOT=/app, index.html=/app/index.html. SA는 Cloud Run에서 ADC로 주입.
# GEMINI_API_KEY 는 Cloud Run 환경변수/시크릿으로 주입.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
