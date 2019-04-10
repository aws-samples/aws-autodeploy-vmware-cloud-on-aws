# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.


# portions of this makefile were based from https://gist.github.com/mpneuried/0594963ad38e68917ef189b4e6a269db

ifndef APP_NAME
        APP_NAME = lambda
endif
ZIP_FILE = lambda.zip
S3_BUCKET_NAME = devint-bryawood-lambda
S3_OBJECT_NAME = lambda5.zip

#cnf ?= config.env
#include $(cnf)
#export $(shell sed 's/=.*//' $(cnf))

# HELP
# This will output the help for each task
# thanks to https://marmelab.com/blog/2016/02/29/auto-documented-makefile.html
.PHONY: help

help:   ## Print this help (default target)
	@awk 'BEGIN {FS = ":.*?## "} /^[a-z\/\.A-Z_-]+:.*?## / {printf "%s|%s\n", $$1, $$2}' \
	    $(MAKEFILE_LIST) | column -ts\|

.DEFAULT_GOAL := help
.ONESHELL:
.INTERMEDIATE: build

# DOCKER TASKS
clean:
	docker stop $(APP_NAME)
	docker rm $(APP_NAME)
	docker rmi $(APP_NAME)
	rm -f docker/Dockerfile \
	    docker/.docker-install-touchfile \
	    docker/.s3copy-touchfile \
	    docker/container_volume/$(ZIP_FILE)
	rm -rf docker/vsphere-automation-sdk-python
	sudo rm -rf docker/container_volume/__pycache__

docker/.docker-install-touchfile:
	sudo yum update -y
	sudo yum install -y docker git
	sudo usermod -a -G docker $(USER)
	mkdir -p docker/container_volume
	touch docker/.docker-install-touchfile

docker/vsphere-automation-sdk-python:
	cd docker && \
	    git clone https://github.com/vmware/vsphere-automation-sdk-python.git

docker/Dockerfile: Makefile
	cat <<-EOB > docker/Dockerfile
	FROM python:3.6
	COPY vsphere-automation-sdk-python /vsphere-automation-sdk-python
	RUN mkdir -p /lambda /container_volume
	
	# Install dependencies under /lambda:
	RUN apt-get update -y
	RUN apt-get install zip -y
	RUN pip install -t /lambda --upgrade pip
	RUN pip install -t /lambda requests retry tabulate
	RUN pip install -t /lambda --upgrade \
	    --force-reinstall \
    	    -r /vsphere-automation-sdk-python/requirements.txt \
	    --extra-index-url file:///vsphere-automation-sdk-python/lib
	# zip all dependencies, to be used as Lambda deployment package:
	RUN cd /lambda && zip -r9 /$(ZIP_FILE) .
	
	WORKDIR /container_volume
	# add /lambda to PYTHONPATH for ad-hoc/interactive work:
	ENV PYTHONPATH $PYTHONPATH:/lambda
	CMD ["/bin/bash"]
	
	# Example Session:
	#
	# [ec2-user@ip-10-51-3-121 ~]$ make run
	# docker run -iv /home/ec2-user/docker/d/docker/container_volume:/container_volume -t --rm --name="lambda-run" lambda
	# root@41615cf1f660:/container_volume# python3
	# Python 3.6.6 (default, Jul  4 2018, 02:29:03)
	# [GCC 6.3.0 20170516] on linux
	# Type "help", "copyright", "credits" or "license" for more information.
	# >>>
	# >>> import awsvmc
	# >>>
	# >>> v = awsvmc.VMC('your_OAuth_refresh_token')
	# >>> o = awsvmc.ORG(v,'00000000-0000-0000-0000-000000000000')
	# >>> o.listSddcs()
	#
	# SddcId                                Name                Created     Updated     State
	# ------------------------------------  ------------------  ----------  ----------  -------
	# 312e7e05-60eb-4311-a596-f606e0ef03b5  HAL9000             06/25/2018  07/04/2018  READY
	#
	# >>>
	# >>> s = awsvmc.SDDC(o,'312e7e05-60eb-4311-a596-f606e0ef03b5')
	# >>> vc = awsvmc.VC(s)
	# >>> vc.listContentLibraries()
	#
	#  Name                      Created     Synced      SubscriptionURL
	#  ------------------------  ----------  ----------  -----------------------------------------------
	#  Lab Content Library       06/22/2018  07/04/2018  https://s3-us-west-2.amazonaws.com/.../lib.json
	#
	#  >>>
	
	EOB

docker/container_volume/$(ZIP_FILE) : | build

build: docker/.docker-install-touchfile docker/Dockerfile docker/vsphere-automation-sdk-python ## Build the container image
	cd docker && \
	    docker build -t $(APP_NAME) .
	docker run -v $(PWD)/docker/container_volume:/container_volume \
	    --rm --name="$(APP_NAME)-run" $(APP_NAME) cp /$(ZIP_FILE) /container_volume

build-nc: docker/.docker-install-touchfile docker/Dockerfile docker/vsphere-automation-sdk-python ## Build the container image without caching
	cd docker && \
	    docker build --no-cache -t $(APP_NAME) .
	docker run -v $(PWD)/docker/container_volume:/container_volume \
	    --rm --name="$(APP_NAME)-run" $(APP_NAME) cp /$(ZIP_FILE) /container_volume

s3copy: docker/container_volume/$(ZIP_FILE) ## Copy deployment package to S3
	aws s3 cp docker/container_volume/$(ZIP_FILE) s3://$(S3_BUCKET_NAME)/$(S3_OBJECT_NAME) && \
	    touch docker/.s3copy-touchfile

create: docker/container_volume/$(ZIP_FILE) ## Create container from image
	docker create -iv $(PWD)/docker/container_volume:/container_volume \
	    --name $(APP_NAME) $(APP_NAME) /bin/bash
	docker start $(APP_NAME)

run: docker/container_volume/$(ZIP_FILE)  ## Run container from image
	docker run -iv $(PWD)/docker/container_volume:/container_volume -t \
	    --rm --name="$(APP_NAME)-run" $(APP_NAME)

interact: docker/container_volume/$(ZIP_FILE)  ## Run container, launch ./interact.py script
	docker run -iv $(PWD)/docker/container_volume:/container_volume -t \
	    --rm --name="$(APP_NAME)-run" $(APP_NAME) ./interact.py

up: docker/container_volume/$(ZIP_FILE) run ## Build then run container

stop:   ## Stop and remove a running container
	docker stop $(APP_NAME); docker rm $(APP_NAME)

status: | docker/container_status docker/image_status ## Run docker container ls -a ; docker container image ls -a
docker/container_status:
	docker container ls -a
docker/image_status:
	docker image ls -a

