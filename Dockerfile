FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 设置时区为中国时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

ENV PATH="/usr/local/bin:${PATH}"

# 复制项目文件
COPY main.py push.py config.py log_utils.py scheduler.py requirements.txt ./

# 创建日志目录并设置权限
RUN mkdir -p /app/logs && chmod 777 /app/logs

# 安装 Python 依赖（版本在 requirements.txt 中锁定）
RUN pip install --no-cache-dir -r requirements.txt

# 启动命令（由 scheduler.py 按 CRON_SCHEDULE 调度执行 main.py）
CMD ["python", "scheduler.py"]
