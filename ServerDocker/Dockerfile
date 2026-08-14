FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "discord.py[voice]>=2.5" aiohttp
COPY agent.py .
VOLUME /data
EXPOSE 8766
CMD ["python", "-u", "agent.py"]
