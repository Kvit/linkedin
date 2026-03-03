FROM mcr.microsoft.com/devcontainers/python:2-3.13-bookworm

# Remove Yarn repo with expired GPG key
RUN rm -f /etc/apt/sources.list.d/yarn.list

# install midnight commander
RUN apt-get update && apt-get install -y mc

# install npx
RUN curl -fsSL https://deb.nodesource.com/setup_lts.sh | bash - && \
    apt-get install -y nodejs

# install uv package manager
RUN pip install uv
RUN uv tool install ruff

# Install ngrok
RUN curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
    | tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null \
    && echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
    | tee /etc/apt/sources.list.d/ngrok.list \
    && apt-get update && apt-get install -y ngrok

# Install Google Cloud SDK
RUN curl https://sdk.cloud.google.com | bash && exec bash
ENV PATH="/root/google-cloud-sdk/bin:${PATH}"

# add google auth credentials
# COPY pp-recovr-master.json /etc/credentials.json
# ENV GOOGLE_APPLICATION_CREDENTIALS="/etc/credentials.json"

# Expose the port
EXPOSE 5433
