FROM mcr.microsoft.com/devcontainers/python:1-3.12-bullseye

# Install additional python modules
RUN pip install --upgrade pip

#set the working directory
WORKDIR /etc

# Copy requirements.txt to the container
COPY requirements.txt .

# Install the requirements
RUN pip install -r requirements.txt

# add google auth credentials
# COPY pp-recovr-master.json /etc/credentials.json
# ENV GOOGLE_APPLICATION_CREDENTIALS="/etc/credentials.json"
