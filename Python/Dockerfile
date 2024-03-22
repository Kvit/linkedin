FROM mcr.microsoft.com/devcontainers/python:1-3.12-bullseye

# Install additional python modules
RUN pip install --upgrade pip

#set the working directory
WORKDIR /etc

# Copy requirements.txt to the container
COPY requirements.txt .

# Install the requirements
RUN pip install -r requirements.txt

# install Google Cloud SDK
RUN apt-get update && apt-get install -y curl gnupg && \
    export CLOUD_SDK_REPO="cloud-sdk-$(lsb_release -c -s)" && \
    echo "deb http://packages.cloud.google.com/apt $CLOUD_SDK_REPO main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | apt-key add - && \
    apt-get update -y && apt-get install google-cloud-sdk -y


# add google auth credentials
# COPY pp-recovr-master.json /etc/credentials.json
# ENV GOOGLE_APPLICATION_CREDENTIALS="/etc/credentials.json"
