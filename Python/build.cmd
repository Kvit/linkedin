echo "building base image"
docker build   -t linkedin:base .
echo "building deployment image"
docker build   -t linkedin:latest . -f Dockerfile.deploy