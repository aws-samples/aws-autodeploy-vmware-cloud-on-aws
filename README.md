## AWS Autodeploy Vmware Cloud On AWS

VMware Cloud on AWS sample provisioning automation

## License Summary

This sample code is made available under a modified MIT license. See the LICENSE file.

Deploys Software Defined DataCenters (SDDCs) using VMware's Software Developer Kit.  

Prerequisites:

	1. Administrative Access to an existing AWS Customer Account
	2. An existing Amazon Linux EC2 instance, with an an 
	   attached instance role with S3 read and write access
	   to an existing named bucket
	3. An existing https://vmc.vmware.com VMware account that has
	   been invited into a VMWonAWS Organization (the Org ID must
	   be inserted into a config.json file for end to end automation)
	4. An OAuth Refresh Token must be created at https://vmc.vmware.com
	   which grants programmatic API access to VMWonAWS (the refreshToken
	   must also be inserted into the config.json file)
	5. The VMWonAWS Organization must be pre-connected to your AWS Customer
	   Account (the selected AWS Subnet ID(s) at the time the account 
	   was connected must also be inserted into the config.json file)

The toplevel Makefile is intended to be used within an Amazon Linux EC2 instance.  Available Targets:

	$ find .
	./Makefile
	./docker
	./docker/container_volume
	./docker/container_volume/config.json.example
	./docker/container_volume/awsvmc.py
	./docker/container_volume/interact.py
	 [..]
	$ make
	help      Print this help (default target)
	<b>build</b>     Build the container image
	build-nc  Build the container image without caching
	create    Create container from image
	run       Run container from image
	interact  Run container, launch ./interact.py script
	up        Build then run container
	stop      Stop and remove a running container
	status    Run docker container ls -a ; docker container image ls -a

A simple <b>make build</b> will install docker, download the vSphere Automation SDK, and build a docker image containing all of the python prerequisites.  The ./docker/container_volume directory is shared with the container, and contains the primary awsvmc python module, along with an interactive script for demonstrating its capabilities (interact.py).  There is also also a config.json.sample file that must be copied to config.json, and updated with your VMware Cloud on AWS Organziation ID and OAuth Refresh token values.

	./step-function
	./step-function/VMware-Cloud-on-AWS-AutoDeploy.step-function.json
	./step-function/VMware-Cloud-on-AWS-AutoDeploy.lambda_function.py

An additional <b>./step-function</b> directory includes a lambda_function.py, and a Step-Function state machine json definition.  The lambda_function.py is kept small by downloading the deployment payload containing its dependencies via s3.boto when the container is first initialized.  All of the Lambda steps in the Step-Function state machine point to this same Lambda function, where the different tasks are triggered by input event context payload.

With the prerequisites in place, SDDCs defined in config.json can be selectively provisioned by invoking the step-function workflow.  A sample CloudFormation script to trigger the Lambda/Step-Function workflow via Custom Resource is also provided, which can also optionally be imported into a Service Catalog Portfolio as a versioned Product.

