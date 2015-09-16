Wercker step for AWS ECS
=======================

This wercker step allows to deploy Docker containers with [AWS ECS](http://docs.aws.amazon.com/AmazonECS/latest/developerguide/ECS_GetStarted.html) service.

Please read the [AWS ECS](http://docs.aws.amazon.com/AmazonECS/latest/developerguide/Welcome.html) documentation and [API](http://docs.aws.amazon.com/AmazonECS/latest/APIReference/Welcome.html) before using this step.


The step install the [AWS Cli](http://docs.aws.amazon.com/cli/latest/userguide/installing.html) through pip, if the CLI is not already installed. 


## AWS ECS workflow

To deploy an application with AWS ECS, the Wercker step follow this steps :

#### Step 1 : [Configuring AWS](http://docs.aws.amazon.com/cli/latest/reference/configure/index.html)

This initial step consists on configuring AWS.

The following configuration allows to setup this step :

* `key` (required): AWS Access Key ID
* `secret` (required): AWS Secret Access Key
* `region` (optional): Default region name

#### Step 2 : [Checking ECS Cluster](http://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_DescribeClusters.html)

This step check a previously created ECS cluster exists.

The following configuration allows to setup this step :

* `cluster-name` (required): The name of the cluster to deploy the service


#### Step 3 : [Checking ECS Service](http://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_DescribeServices.html)

This step check a previously created ECS service exists.

The following configuration allows to setup this step :

* `service-name` (required): The name of the service to deploy


#### Step 4 : [Create New Task Definition ](http://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_RegisterTaskDefinition.html)

This step register a new task definition for the service.

The following configuration allows to setup this step :

* `task-definition-name` (required): The name of the task definition
* `task-definition-file` (required): The file containing the task definition


#### Step 5 : [Scale Down ECS Service](http://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_UpdateService.html)

This step scale down the service in order to deploy the new revision.

See this [thread](https://forums.aws.amazon.com/thread.jspa?threadID=179271) for explanation :
 
 
> The service scheduler will always scale up at least 1 new container before shutting down old containers. 
This means on an update, if your current service uses all resources in your cluster you may actually need to scale your service to N-1 where N is your current running task count for that service.
For example, if you have 5 container instances and 5 tasks running bound to host port 80, in order to perform a rolling update you'll need to set the desired count on the service to 4. 
The scheduler will recognize that it can stop 1 container and still meet your needs. After stopping 1, the free space will be used to start another. 
You can think of desired count as also the minimum count for now and the scheduler won't remove tasks below that minimum which is likely why you see resources not found.


#### Step 6 : [Update ECS Service](http://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_UpdateService.html)

This step update the service with the new revision.
 
 
## Example

The following example deploy an `hello` service to ECS :

```
deploy:
  steps:
    - 1science/aws-ecs:
      key: aws_access_key_id
      secret: aws_access_secret_id
      cluster-name: staging
      service-name: hello
      task-definition-name: hello
      task-definition-file: /app/hello-task-definition.json
```