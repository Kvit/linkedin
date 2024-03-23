echo running linkedin container with mapped google credentials
docker run -it --rm -p 8080:8080 -v D:\Code\linkedin\Python\vk-linkedin-master-service-account.json:/app/vk-linkedin-master-service-account.json linkedin:latest
