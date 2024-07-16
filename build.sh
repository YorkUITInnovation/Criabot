#!/bin/bash

docker buildx build --push \
--platform linux/amd64,linux/arm64 \
--tag uitadmin/criabot:latest \
--tag uitadmin/criabot:v1.6.6 .