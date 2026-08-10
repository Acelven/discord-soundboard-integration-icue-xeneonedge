FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "discord.py[voice]>=2.5" aiohttp
COPY agent.py .
VOLUME /data
EXPOSE 8766
CMD ["python", "-u", "agent.py"]
