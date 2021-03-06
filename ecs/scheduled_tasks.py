# coding: utf-8
import json
import logging
import copy
import jinja2
import enum
from datadiff import diff
from distutils.util import strtobool

import ecs.classes
import render
from ecs.utils import adjust_container_definition, is_same_container_definition, get_variables
from ecs.classes import Deploy, DeployTargetType

logger = logging.getLogger(__name__)

scheduled_task_managed_description = "MANAGED BY TASK MANAGER"


class ParameterNotFoundException(Exception):
    pass


class ParameterInvalidException(Exception):
    pass


class VariableNotFoundException(Exception):
    pass


class EnvironmentValueNotFoundException(Exception):
    pass


class CloudWatchEventState(enum.Enum):
    enabled = 'ENABLED'
    disabled = 'DISABLED'

    @staticmethod
    def get_state(state: str):
        if state == CloudWatchEventState.enabled.value:
            return CloudWatchEventState.enabled
        elif state == CloudWatchEventState.disabled.value:
            return CloudWatchEventState.disabled
        raise Exception('CloudWatch Event no such state {state}'.format(state=state))


class TaskEnvironment(object):
    def __init__(self, task_definition: dict) -> None:
        try:
            task_environment_list = task_definition['containerDefinitions'][0]['environment']
        except:
            raise EnvironmentValueNotFoundException(
                "task definition is lack of environment.\ntask definition:\n{task_definition}"
                .format(task_definition=task_definition))

        self.environment = None
        self.cluster_name = None
        self.service_group = None
        self.template_group = None
        self.task_count = None
        self.placement_strategy = None
        self.placement_constraints = None
        for task_environment in task_environment_list:
            if task_environment['name'] == 'ENVIRONMENT':
                self.environment = task_environment['value']
            if task_environment['name'] == 'CLUSTER_NAME':
                self.cluster_name = task_environment['value']
            elif task_environment['name'] == 'SERVICE_GROUP':
                self.service_group = task_environment['value']
            elif task_environment['name'] == 'TEMPLATE_GROUP':
                self.template_group = task_environment['value']
            elif task_environment['name'] == 'TASK_COUNT':
                self.task_count = int(task_environment['value'])
            elif task_environment['name'] == 'TARGET_LAMBDA_ARN':
                self.target_lambda_arn = task_environment['value']
        if self.environment is None:
            raise EnvironmentValueNotFoundException(
                "task definition is lack of environment `ENVIRONMENT`.\ntask definition:\n{task_definition}"
                .format(task_definition=task_definition))
        elif self.cluster_name is None:
            raise EnvironmentValueNotFoundException(
                "task definition is lack of environment `CLUSTER_NAME`.\ntask definition:\n{task_definition}"
                .format(task_definition=task_definition))
        elif self.task_count is None:
            raise EnvironmentValueNotFoundException(
                "task definition is lack of environment `TASK_COUNT`.\ntask definition:\n{task_definition}"
                .format(task_definition=task_definition))
        elif self.target_lambda_arn is None:
            raise EnvironmentValueNotFoundException(
                "task definition is lack of environment `TARGET_LAMBDA_ARN`.\ntask definition:\n{task_definition}"
                .format(task_definition=task_definition))


class CloudwatchEventRule(Deploy):
    def __init__(self, rule: dict):
        self.name = rule['Name']
        self.arn = rule['Arn']
        self.state = CloudWatchEventState.get_state(rule['State'])
        self.description = rule['Description']
        self.scheduled_expression = rule['ScheduleExpression']

        self.task_definition = None
        self.task_definition_arn = None
        self.task_environment = None
        self.family = None

        super().__init__(self.name, target_type=DeployTargetType.scheduled_task)

    def set_from_task_definition(self, task_definition: dict):
        self.task_definition = task_definition
        self.task_definition_arn = task_definition.get('taskDefinitionArn')
        self.task_environment = TaskEnvironment(task_definition)
        self.family = task_definition['family']


class ScheduledTask(Deploy):
    def __init__(self, task_definition, target_lambda_arn, schedule_expression, placement_strategy, placement_constraints):
        self.task_definition = task_definition
        self.family = task_definition.get('family')
        if self.family is None:
            raise EnvironmentValueNotFoundException(
                "task definition parameter `family` no found.\ntask definition:\n{task_definition}"
                .format(task_definition=task_definition))

        self.task_environment = TaskEnvironment(task_definition)
        self.target_lambda_arn = target_lambda_arn
        self.schedule_expression = schedule_expression
        self.placement_strategy = placement_strategy
        self.placement_constraints = placement_constraints

        self.status = ecs.classes.ProcessStatus.normal

        self.state = CloudWatchEventState.enabled
        self.task_exists = False
        self.origin_task_definition_arn = None
        self.origin_task_definition = None
        self.origin_task_environment = None
        self.task_definition_arn = None

        super().__init__(self.family, target_type=DeployTargetType.scheduled_task)

    def set_from_cloudwatch_event_rule(self, cloudwatch_event_rule: CloudwatchEventRule):
        self.origin_task_definition = cloudwatch_event_rule.task_definition
        self.origin_task_definition_arn = cloudwatch_event_rule.task_definition_arn
        self.origin_task_environment = cloudwatch_event_rule.task_environment
        self.state = cloudwatch_event_rule.state
        self.task_exists = True

    def compare_container_definition(self):
        if self.is_same_task_definition():
            self.task_definition_arn = self.origin_task_definition_arn
            return "    - Container Definition is not changed."
        else:
            ad = adjust_container_definition(self.origin_task_definition['containerDefinitions'])
            bd = adjust_container_definition(self.task_definition['containerDefinitions'])
            t = diff(ad, bd)
            return "    - Container is changed. Diff:\n{t}".format(t=t)

    def is_same_task_definition(self):
        ad = adjust_container_definition(self.origin_task_definition['containerDefinitions'])
        bd = adjust_container_definition(self.task_definition['containerDefinitions'])
        return is_same_container_definition(ad, bd)


def get_deploy_scheduled_task_list(task_list, deploy_service_group, template_group):
    if deploy_service_group is not None:
        deploy_service_list = list(filter(
            lambda service: service.task_environment.service_group == deploy_service_group, task_list))
    else:
        deploy_service_list = copy.copy(task_list)

    if template_group is not None:
        deploy_service_list = list(filter(
            lambda service: service.task_environment.template_group == template_group, deploy_service_list))

    return deploy_service_list


def get_scheduled_task_list(services_config,
                            environment_config,
                            is_task_definition_config_env: bool,
                            environment):
    try:
        scheduled_tasks = services_config["scheduledTasks"]
    except KeyError:
        return []
    task_definition_template_dict = services_config["taskDefinitionTemplates"]

    scheduled_task_list = []
    scheduled_task_name_list = []
    for task_name in scheduled_tasks:
        if task_name in scheduled_task_list:
            raise Exception("'%s' is duplicate task." % task_name)
        scheduled_task_name_list.append(task_name)
        # 設定値と変数を取得
        task_config, variables = get_variables(
            deploy_name = 'scheduledTasks',
            name=task_name,
            base_service_config=scheduled_tasks.get(task_name),
            environment_config=environment_config,
            is_task_definition_config_env=is_task_definition_config_env
        )

        # parameter check & build docker environment
        env = [{"name": "ENVIRONMENT", "value": environment}]

        cluster = task_config.get("cluster")
        if cluster is None:
            raise ParameterNotFoundException("Service `{task_name}` requires parameter `cluster`"
                                             .format(task_name=task_name))
        cluster = render.render_template(str(cluster), variables, is_task_definition_config_env)
        env.append({"name": "CLUSTER_NAME", "value": cluster})

        service_group = task_config.get("serviceGroup")
        if service_group is not None:
            service_group = render.render_template(str(service_group), variables, is_task_definition_config_env)
            env.append({"name": "SERVICE_GROUP", "value": service_group})

        template_group = task_config.get("templateGroup")
        if template_group is not None:
            template_group = render.render_template(str(template_group), variables, is_task_definition_config_env)
            env.append({"name": "TEMPLATE_GROUP", "value": template_group})

        task_count = task_config.get("taskCount")
        if task_count is None:
            raise ParameterNotFoundException("Scheduled Task `{task_name}` requires parameter `desiredCount`"
                                             .format(task_name=task_name))
        task_count = render.render_template(str(task_count), variables, is_task_definition_config_env)
        try:
            int(task_count)
        except ValueError:
            raise ParameterInvalidException("Scheduled Task `{task_name}` parameter `taskCount` is int"
                                            .format(task_name=task_name))
        env.append({"name": "TASK_COUNT", "value": task_count})

        placement_strategy = task_config.get("placementStrategy")
        placement_strategy_list = None
        if placement_strategy is not None:
            placement_strategy_list = []
            for strategy in placement_strategy:
                strategy = render.render_template(json.dumps(strategy), variables, is_task_definition_config_env)
                strategy = json.loads(strategy)
                placement_strategy_list.append(strategy)
            env.append({"name": "PLACEMENT_STRATEGY", "value": str(placement_strategy)})

        placement_constraints = service_config.get("placementConstraints")
        placement_constraints_list = None
        if placement_constraints is not None:
            placement_constraints_list = []
            for constrant in placement_constraints:
                constrant = render.render_template(json.dumps(constrant), variables, is_task_definition_config_env)
                constrant = json.loads(constrant)
                placement_constraints_list.append(constrant)
            env.append({"name": "PLACEMENT_CONSTRAINTS", "value": str(placement_constraints)})

        cloudwatch_event = task_config.get('cloudwatchEvent')
        if cloudwatch_event is None:
            raise ParameterNotFoundException("Scheduled Task `{task_name}` requires parameter `cloudwatchEvent`"
                                             .format(task_name=task_name))
        schedule_expression = cloudwatch_event.get("scheduleExpression")
        if schedule_expression is None:
            raise ParameterNotFoundException("Scheduled Task `{task_name}` requires parameter "
                                             "`cloudwatchEvent.scheduleExpression`"
                                             .format(task_name=task_name))
        schedule_expression = render.render_template(
            str(schedule_expression),
            variables,
            is_task_definition_config_env
        )

        target_lambda_arn = cloudwatch_event.get("targetLambdaArn")
        if schedule_expression is None:
            raise ParameterNotFoundException("Scheduled Task `{task_name}` requires parameter "
                                             "`cloudwatchEvent.targetLambdaArn`"
                                             .format(task_name=task_name))
        target_lambda_arn = render.render_template(str(target_lambda_arn), variables, is_task_definition_config_env)
        env.append({"name": "TARGET_LAMBDA_ARN", "value": target_lambda_arn})

        task_definition_template = task_config.get("taskDefinitionTemplate")
        if task_definition_template is None:
            raise ParameterNotFoundException(
                "Scheduled Task `{task_name}` requires parameter `taskDefinitionTemplate`".format(task_name=task_name))
        scheduled_task_definition_template = task_definition_template_dict.get(task_definition_template)
        if scheduled_task_definition_template is None or len(scheduled_task_definition_template) == 0:
            raise Exception("Scheduled Task '%s' taskDefinitionTemplate not found. " % task_name)
        if not isinstance(scheduled_task_definition_template, str):
            raise Exception(
                "Scheduled Task '{task_name}' taskDefinitionTemplate specified template value must be str. "
                .format(task_name=task_name))

        try:
            task_definition_data = render.render_template(scheduled_task_definition_template, variables,
                                                          is_task_definition_config_env)
        except jinja2.exceptions.UndefinedError:
            logger.error("Scheduled Task `%s` jinja2 varibles Undefined Error." % task_name)
            raise
        try:
            task_definition = json.loads(task_definition_data)
        except json.decoder.JSONDecodeError as e:
            raise Exception(
                "Scheduled Task `{task_name}`: {e.__class__.__name__} {e}\njson:\n{task_definition_data}"
                .format(task_name=task_name, e=e, task_definition_data=task_definition_data))

        # set parameters to docker environment
        for container_definitions in task_definition.get("containerDefinitions"):
            task_environment = container_definitions.get("environment")
            container_env = copy.copy(env)
            if task_environment is not None:
                if not isinstance(task_environment, list):
                    raise Exception(
                        "Scheduled Task '{task_name}' taskDefinitionTemplate environment value must be list. "
                        .format(task_name=task_name))
                container_env.extend(task_environment)
            container_definitions["environment"] = container_env

        # disabledになったらリストから外す
        disabled = task_config.get("disabled")
        if disabled is not None:
            disabled = render.render_template(str(disabled), variables, is_task_definition_config_env)
            try:
                disabled = bool(strtobool(disabled))
            except ValueError:
                raise ParameterInvalidException("Scheduled Task `{task_name}` parameter `disabled` must be bool"
                                                .format(task_name=task_name))
            if disabled:
                continue

        scheduled_task = ScheduledTask(
            task_definition=task_definition,
            target_lambda_arn=target_lambda_arn,
            schedule_expression=schedule_expression,
            placement_strategy=placement_strategy,
            placement_constraints=placement_constraints
        )
        scheduled_task_list.append(scheduled_task)

    return scheduled_task_list
