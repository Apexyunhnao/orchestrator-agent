FROM python:3.11-slim

WORKDIR /app

# 依赖：用生成式锁文件安装（含全部传递依赖），保证镜像可复现。
# requirements.txt = 人读的直接依赖清单；requirements.lock.txt = uv pip compile 生成，勿手改。
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
