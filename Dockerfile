FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fleet_manager ./fleet_manager
EXPOSE 8800
CMD ["uvicorn", "fleet_manager.app:app", "--host", "0.0.0.0", "--port", "8800"]
