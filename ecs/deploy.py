# coding: utf-8
import json
import os
import time
import traceback
import sys
from queue import Queue, Empty
from threading import Thread
from botocore.exceptions import WaiterError
import yaml
import yamlordereddictloader

import render
from aws import AwsUtils, EcsServiceNotFoundException, CloudwatchEventRuleNotFoundException
from ecs.classes import ProcessMode, ProcessStatus, VariableNotFoundException
from ecs.scheduled_tasks import ScheduledTask, get_scheduled_task_list, get_deploy_scheduled_task_list, \
    CloudwatchEventRule, CloudWatchEventState, scheduled_task_managed_description
import ecs.service
from ecs.utils import h1, h2, success, error, info


class DeployProcess(Thread):
    def __init__(self, task_queue, key, secret, region, is_service_zero_keep, is_stop_before_deploy,
                 is_service_update_only, is_task_definition_update_only, service_wait_max_attempts, service_wait_delay):
        super().__init__()
        self.task_queue = task_queue
        self.awsutils = AwsUtils(access_key=key, secret_key=secret, region=region)
        self.is_service_zero_keep = is_service_zero_keep
        self.is_stop_before_deploy = is_stop_before_deploy
        self.is_service_update_only = is_service_update_only
        self.is_task_definition_update_only = is_task_definition_update_only
        self.service_wait_max_attempts = service_wait_max_attempts
        self.service_wait_delay = service_wait_delay

    def run(self):
        while True:
            try:
                deploy, mode = self.task_queue.get_nowait()
            except Empty:
                time.sleep(1)
                continue
            # noinspection PyBroadException
            try:
                self.process(deploy, mode)
            except Exception:
                deploy.status = ProcessStatus.error
                error("Unexpected error in `{deploy.name}`.\n{traceback}"
                      .format(deploy=deploy, traceback=traceback.format_exc()))
            finally:
                self.task_queue.task_done()

    def process(self, deploy, mode):
        if deploy.status == ProcessStatus.error:
            error("`{deploy.name}` previous process error. skipping.".format(deploy=deploy))
            return

        if mode == ProcessMode.fetchServices:
            self.fetch_service(deploy)

        elif mode == ProcessMode.deployService:
            self.process_service(deploy)

        elif mode == ProcessMode.checkDeployService:
            self.check_deploy_service(deploy)

        elif mode == ProcessMode.waitForStable:
            wait_for_stable(
                awsutils=self.awsutils,
                service=deploy,
                max_attempts=self.service_wait_max_attempts,
                delay=self.service_wait_delay
            )

        elif mode == ProcessMode.deployScheduledTask:
            self.deploy_scheduled_task(deploy)

        elif mode == ProcessMode.fetchCloudwatchEvents:
            self.fetch_cloudwatch_event(deploy)

        elif mode == ProcessMode.checkDeployScheduledTask:
            self.check_deploy_scheduled_task(deploy)

        elif mode == ProcessMode.stopScheduledTask:
            self.stop_scheduled_task(deploy)

        elif mode == ProcessMode.stopBeforeDeploy:
            self.stop_before_deploy(deploy)

        elif mode == ProcessMode.deleteService:
            self.delete_service(deploy)

    def stop_before_deploy(self, service: ecs.service.Service):
        self.__update_service(
            service=service,
            desired_count=0,
            force_new_deployment=False,
            is_stop_before_deploy=True,
        )
        success("Stop Service '{service.service_name}' succeeded.\n\033[39m"
                "    - 0 task desired"
                .format(service=service))

    def stop_scheduled_task(self, scheduled_task: ScheduledTask):
        if not scheduled_task.task_exists:
            return
        if scheduled_task.state == CloudWatchEventState.enabled:
            self.awsutils.disable_rule(name=scheduled_task.family)
        running_task_arns = self.awsutils.list_running_tasks(
            cluster=scheduled_task.origin_task_environment.cluster_name,
            family=scheduled_task.family
        )
        if len(running_task_arns) > 0:
            info("Stopping Task `{family}`.".format(family=scheduled_task.family))
            for task_arn in running_task_arns:
                self.awsutils.stop_task(
                    cluster=scheduled_task.origin_task_environment.cluster_name,
                    task_arn=task_arn
                )
            self.awsutils.wait_for_task_stopped(
                cluster=scheduled_task.origin_task_environment.cluster_name,
                tasks=running_task_arns
            )

    def fetch_cloudwatch_event(self, cloud_watch_event_rule: CloudwatchEventRule):
        task_definition = self.__describe_task_definition(name=cloud_watch_event_rule.name)
        cloud_watch_event_rule.set_from_task_definition(task_definition)

    def deploy_scheduled_task(self, scheduled_task: ScheduledTask):
        if not scheduled_task.is_same_task_definition():
            res_reg = self.awsutils.register_task_definition(task_definition=scheduled_task.task_definition)
            scheduled_task.task_definition_arn = res_reg['taskDefinitionArn']
        self.awsutils.create_scheduled_task(
            scheduled_task=scheduled_task, description=scheduled_task_managed_description)

        message = """Deploy Scheduled Task '{scheduled_task.name}' succeeded.\033[39m
   - Cloudwatch Event State: {scheduled_task.state.value}"""\
            .format(scheduled_task=scheduled_task)

        if scheduled_task.is_same_task_definition():
            message += """
   - task definition is same. Did not register."""
        else:
            message += """
   - Registering task definition arn: '{task_definition_arn}'"""\
                .format(task_definition_arn=scheduled_task.task_definition_arn)
        message += """
   - schedule '{scheduled_task.schedule_expression}'.
   - {scheduled_task.task_environment.task_count} task count."""\
            .format(scheduled_task=scheduled_task)

        success(message)

    def process_service(self, service: ecs.service.Service):
        message = None
        if not self.is_service_update_only:
            self.__register_task_definition(service)
        if not self.is_task_definition_update_only:
            self.__update_service(service=service, desired_count=service.task_environment.desired_count)
            message = """Deploy Service '{service.service_name}' succeeded.\033[39m""".format(service=service)
        if not self.is_service_update_only:
            if message is None:
                message = """Register Task Definition '{service.service_name}.\033[39m'""".format(service=service)
            if service.is_same_task_definition():
                message += """
   - task definition is same. Did not register."""
            else:
                message += """
   - Registering task definition arn: '{task_definition_arn}'"""\
                .format(task_definition_arn=service.task_definition_arn)
            message += """
    - {service.task_environment.desired_count:d} task desired""".format(service=service)

        success(message)

    def fetch_service(self, describe_service: ecs.service.DescribeService):
        task_definition = self.__describe_task_definition(name=describe_service.task_definition_arn)
        describe_service.set_from_task_definition(task_definition)

    def check_deploy_service(self, service: ecs.service.Service):
        if service.origin_task_definition_arn is None:
            try:
                res_service = self.awsutils.describe_service(
                    service.task_environment.cluster_name, service.service_name
                )
                describe_service = ecs.service.DescribeService(service_description=res_service)
                task_definition = self.__describe_task_definition(describe_service.task_definition_arn)
                describe_service.set_from_task_definition(task_definition)
                service.set_from_describe_service(describe_service=describe_service)
            except EcsServiceNotFoundException:
                error("Service '{service.service_name}' not Found. will be created.".format(service=service))
                return
        if not service.origin_service_exists:
            error("Service '{service.service_name}' status not Active. will be recreated.".format(service=service))
            return

        checks = service.compare_container_definition()

        success("Checking service '{service.service_name}' succeeded "
                "({service.running_count:d} / {service.desired_count:d})\n\033[39m{checks}"
                .format(service=service, checks=checks))

    def check_deploy_scheduled_task(self, scheduled_task: ScheduledTask):
        if scheduled_task.origin_task_definition_arn is None:
            try:
                describe_rule = self.awsutils.describe_rule(scheduled_task.name)
                task_definition = self.__describe_task_definition(scheduled_task.name)
                c = CloudwatchEventRule(describe_rule)
                c.set_from_task_definition(task_definition)
                scheduled_task.set_from_cloudwatch_event_rule(c)
            except CloudwatchEventRuleNotFoundException:
                error("Scheduled Task '{scheduled_task.name}' not Found. will be created."
                      .format(scheduled_task=scheduled_task))
                return

        checks = scheduled_task.compare_container_definition()

        success("Checking scheduled task '{scheduled_task.name}' succeeded. \n\033[39m{checks}"
                .format(scheduled_task=scheduled_task, checks=checks))

    def delete_service(self, service: ecs.service.Service):
        self.awsutils.delete_service(service.cluster_name, service.service_name)
        success("Delete service '{service.service_name}'".format(service=service))

    def __create_service(self, service: ecs.service.Service, is_stop_before_deploy=False):
        desired_count = service.task_environment.desired_count
        if is_stop_before_deploy:
            desired_count = 0
        res_service = self.awsutils.create_service(
            cluster=service.task_environment.cluster_name,
            service=service.service_name,
            task_definition=service.task_definition_arn,
            desired_count=desired_count,
            maximum_percent=service.task_environment.maximum_percent,
            minimum_healthy_percent=service.task_environment.minimum_healthy_percent,
            distinct_instance=service.task_environment.distinct_instance,
            placement_strategy=service.placement_strategy,
            placement_constraints=service.placement_constraints,
            load_balancers=service.load_balancers,
            network_configuration=service.network_configuration,
            service_registries=service.service_registries,
        )
        service.update_run_count(describe_service=res_service, is_stop_before_deploy=False, is_create_service=True)
        return res_service

    def __update_service(
        self, service: ecs.service.Service, desired_count=None, force_new_deployment=True, is_stop_before_deploy=False
    ):
        if self.is_service_zero_keep and service.origin_desired_count == 0:
            desired_count = 0
        task_definition = service.task_definition_arn
        if task_definition is None:
            task_definition = service.task_definition.get('family')
        try:
            res_service = self.awsutils.update_service(
                cluster=service.task_environment.cluster_name,
                service=service.service_name,
                task_definition=task_definition,
                maximum_percent=service.task_environment.maximum_percent,
                minimum_healthy_percent=service.task_environment.minimum_healthy_percent,
                desired_count=desired_count,
                force_new_deployment=force_new_deployment,
            )
        except EcsServiceNotFoundException:
            error("Service '{service.service_name}' not Found. will be created.".format(service=service))
            self.__register_task_definition(service)
            res_service = self.__create_service(service)
        service.update_run_count(describe_service=res_service, is_stop_before_deploy=is_stop_before_deploy)

    def __describe_task_definition(self, name: str) -> dict:
        task_definition = self.awsutils.describe_task_definition(name=name)
        return task_definition

    def __register_task_definition(self, service: ecs.service.Service):
        # if same task definition, then do not register.
        if service.is_same_task_definition():
            return
        task_definition = self.awsutils.register_task_definition(task_definition=service.task_definition)
        service.set_task_definition_arn(task_definition)


class DeployManager(object):
    def __init__(self, args):
        self._args = args

        self.awsutils = AwsUtils(access_key=args.key, secret_key=args.secret, region=args.region)
        self.task_queue = Queue()

        self.cluster_list = self.awsutils.list_clusters()
        self.threads_count = args.threads_count
        self.service_wait_max_attempts = args.service_wait_max_attempts
        self.service_wait_delay = args.service_wait_delay

        self.key = args.key
        self.secret = args.secret
        self.region = args.region

        self.error = False

        # 削除対象
        self.delete_service_list = []
        # 全サービス
        self.all_service_list = []
        # デプロイ対象の全サービス
        self.all_deploy_target_service_list = []

        # 優先付きのstopBeforeDeployサービス
        self.primary_stop_before_deploy_service_list = []
        # stopBeforeDeployサービス
        self.stop_before_deploy_service_list = []
        # 優先付きのサービス
        self.primary_deploy_service_list = []
        # それ以外
        self.remain_deploy_service_list = []

        self.delete_scheduled_task_list = []
        self.scheduled_task_list = []

        self.environment = None
        self.template_group = None
        self.is_service_zero_keep = True
        self.is_stop_before_deploy = True
        self.is_delete_unused_service = True
        self.is_service_update_only = False
        self.is_task_definition_update_only = False
        self.force = False

    def _service_config(self):
        self.all_service_list,\
            self.all_deploy_target_service_list,\
            self.scheduled_task_list,\
            self.deploy_scheduled_task_list,\
            self.environment = get_deploy_list(
                services_yaml=self._args.services_yaml,
                environment_yaml=self._args.environment_yaml,
                task_definition_template_dir=self._args.task_definition_template_dir,
                task_definition_config_json=self._args.task_definition_config_json,
                task_definition_config_env=self._args.task_definition_config_env,
                deploy_service_group=self._args.deploy_service_group,
                template_group=self._args.template_group
            )
        # thread数がタスクの数を超えているなら減らす
        deploy_size = len(self.deploy_scheduled_task_list) + len(self.all_deploy_target_service_list)
        if deploy_size < self.threads_count:
            self.threads_count = deploy_size
        self.is_service_zero_keep = self._args.service_zero_keep
        self.template_group = self._args.template_group
        self.is_delete_unused_service = self._args.delete_unused_service
        self.is_stop_before_deploy = self._args.stop_before_deploy
        self.is_service_update_only = self._args.service_update_only
        self.is_task_definition_update_only = self._args.task_definition_update_only

    def _set_deploy_list(self):
        for service in self.all_deploy_target_service_list:
            if self.is_stop_before_deploy and service.stop_before_deploy:
                if service.is_primary_placement:
                    self.primary_stop_before_deploy_service_list.append(service)
                else:
                    self.stop_before_deploy_service_list.append(service)
            else:
                if service.is_primary_placement:
                    self.primary_deploy_service_list.append(service)
                else:
                    self.remain_deploy_service_list.append(service)

    def _unstopped_primary_stop_before_deploy_service_list(self) -> list:
        return [x for x in self.primary_stop_before_deploy_service_list]

    def _unstopped_stop_before_deploy_service_list(self) -> list:
        return [x for x in self.stop_before_deploy_service_list]

    def _start_threads(self):
        # threadの開始
        for _ in range(self.threads_count):
            thread = DeployProcess(
                task_queue=self.task_queue,
                key=self.key,
                secret=self.secret,
                region=self.region,
                is_service_zero_keep=self.is_service_zero_keep,
                is_stop_before_deploy=self.is_stop_before_deploy,
                is_service_update_only=self.is_service_update_only,
                is_task_definition_update_only=self.is_task_definition_update_only,
                service_wait_max_attempts=self.service_wait_max_attempts,
                service_wait_delay=self.service_wait_delay
            )
            thread.setDaemon(True)
            thread.start()

    def run(self):
        self._service_config()
        self._start_threads()
        if not self.is_service_update_only:
            self._fetch_ecs_information()

        if not (self.is_service_update_only or self.is_task_definition_update_only):
            # Step: Delete Unused Service
            self._delete_unused()

        if not self.is_service_update_only:
            self._check_deploy()

        self._set_deploy_list()

        if not (self.is_service_update_only or self.is_task_definition_update_only):
            self._stop_scheduled_task()
        if not self.is_task_definition_update_only:
            self._stop_before_deploy()
        self._deploy_service()
        if not self.is_task_definition_update_only:
            self._start_after_deploy()

        if not (self.is_service_update_only or self.is_task_definition_update_only):
            self._deploy_scheduled_task()

        if not self.is_task_definition_update_only:
            self._result_check()

    def dry_run(self):
        self._service_config()
        self._start_threads()
        self._fetch_ecs_information()

        # Step: Check Delete Service
        self._delete_unused(dry_run=True)
        # Step: Check Service
        self._check_deploy()
        self._set_deploy_list()

    def delete(self):
        self.environment = self._args.environment
        self.force = self._args.force
        self._start_threads()
        self._fetch_ecs_information(is_all=True)

        if self.delete_service_list == 0 and self.delete_scheduled_task_list == 0:
            info("No delete service or scheduled task")
            return
        h1("Delete Service or Scheduled Task")
        for service in self.delete_service_list:
            print("* %s" % service.service_name)
        for task in self.delete_scheduled_task_list:
            print("* %s" % task.family)

        if not self.force:
            reply = input("\nWould you like delete all ecs service in %s (y/n)\n" % self.environment)
            if reply != 'y':
                return
        self._delete_unused()

    def _stop_scheduled_task(self):
        if len(self.deploy_scheduled_task_list) > 0:
            h1("Step: Stop ECS Scheduled Task")
            for task in self.deploy_scheduled_task_list:
                self.task_queue.put([task, ProcessMode.stopScheduledTask])
            self.task_queue.join()

    def _stop_before_deploy(self):
        if len(self._unstopped_primary_stop_before_deploy_service_list()) > 0 \
                or len(self._unstopped_stop_before_deploy_service_list()) > 0:
            h1("Step: Stop ECS Service Before Deploy")
            for service in self._unstopped_primary_stop_before_deploy_service_list():
                self.task_queue.put([service, ProcessMode.stopBeforeDeploy])
            for service in self._unstopped_stop_before_deploy_service_list():
                self.task_queue.put([service, ProcessMode.stopBeforeDeploy])
            self.task_queue.join()
            h2("Wait for Service Status 'Stable'")
            self._wait_for_stable(self._unstopped_primary_stop_before_deploy_service_list())
            self._wait_for_stable(self._unstopped_stop_before_deploy_service_list())

    def _start_after_deploy(self):
        if len(self.primary_stop_before_deploy_service_list) > 0:
            h1("Step: Start Primary ECS Service After Deploy")
            for service in self.primary_stop_before_deploy_service_list:
                self.task_queue.put([service, ProcessMode.deployService])
            self.task_queue.join()
            h2("Wait for Service Status 'Stable'")
            self._wait_for_stable(self.primary_stop_before_deploy_service_list)
        if len(self.stop_before_deploy_service_list) > 0:
            h1("Step: Start ECS Service After Deploy")
            for service in self.stop_before_deploy_service_list:
                self.task_queue.put([service, ProcessMode.deployService])
            self.task_queue.join()
            h2("Wait for Service Status 'Stable'")
            self._wait_for_stable(self.stop_before_deploy_service_list)

    def _deploy_scheduled_task(self):
        if len(self.deploy_scheduled_task_list) > 0:
            h1("Step: Deploy ECS Scheduled Task")
            for task in self.deploy_scheduled_task_list:
                self.task_queue.put([task, ProcessMode.deployScheduledTask])
            self.task_queue.join()

    def _delete_unused(self, dry_run=False):
        if dry_run:
            h1("Step: Check Delete Unused")
        else:
            h1("Step: Delete Unused")
        if not self.is_delete_unused_service:
            info("Do not delete unused")
            return
        if len(self.delete_service_list) == 0 and len(self.delete_scheduled_task_list) == 0:
            info("There was no service or task to delete.")
        for service in self.delete_service_list:
            if not dry_run:
                self.task_queue.put([service, ProcessMode.deleteService])
        self.task_queue.join()
        for delete_scheduled_task in self.delete_scheduled_task_list:
            success("Delete scheduled task '{delete_scheduled_task.name}'"
                    .format(delete_scheduled_task=delete_scheduled_task))
            if not dry_run:
                self.awsutils.delete_scheduled_task(
                    name=delete_scheduled_task.name,
                    target_arn=delete_scheduled_task.task_environment.target_lambda_arn
                )

    def _fetch_ecs_information(self, is_all=False):
        h1("Step: Fetch ECS Information")
        describe_service_list = []
        if len(self.all_service_list) > 0 or is_all:
            describe_service_list = ecs.service.fetch_aws_service(
                cluster_list=self.cluster_list, awsutils=self.awsutils
            )
            for s in describe_service_list:
                self.task_queue.put([s, ProcessMode.fetchServices])
        cloud_watch_rule_list = []
        if len(self.scheduled_task_list) > 0 or is_all:
            rules = self.awsutils.list_cloudwatch_event_rules()
            for r in rules:
                if r.get('Description') == scheduled_task_managed_description:
                    c = CloudwatchEventRule(r)
                    cloud_watch_rule_list.append(c)
                    self.task_queue.put([c, ProcessMode.fetchCloudwatchEvents])
        while self.task_queue.qsize() > 0:
            print('.', end='', flush=True)
            time.sleep(3)
        self.task_queue.join()
        info("")

        # set service description and get delete servicelist
        for describe_service in describe_service_list:
            if self.environment != describe_service.task_environment.environment:
                continue
            if self.template_group is not None:
                if self.template_group != describe_service.task_environment.template_group:
                    continue
            is_delete = True
            for service in self.all_service_list:
                if service.service_name == describe_service.service_name:
                    if service.task_environment.cluster_name == describe_service.cluster_name:
                        service.set_from_describe_service(describe_service=describe_service)
                        is_delete = False
                        break
            if is_delete:
                self.delete_service_list.append(describe_service)
        for cloud_watch_rule in cloud_watch_rule_list:
            if self.environment != cloud_watch_rule.task_environment.environment:
                continue
            if self.template_group is not None:
                if self.template_group != cloud_watch_rule.task_environment.template_group:
                    continue
            is_delete = True
            for scheduled_task in self.scheduled_task_list:
                if scheduled_task.family == cloud_watch_rule.family:
                    scheduled_task.set_from_cloudwatch_event_rule(cloud_watch_rule)
                    is_delete = False
                    break
            if is_delete:
                self.delete_scheduled_task_list.append(cloud_watch_rule)
        success("Check succeeded")

    def _deploy_service(self):
        if len(self.primary_deploy_service_list) > 0:
            h1("Step: Deploy Primary ECS Service")
            for service in self.primary_deploy_service_list:
                self.task_queue.put([service, ProcessMode.deployService])
            self.task_queue.join()
            h2("Wait for Service Status 'Stable'")
            self._wait_for_stable(self.primary_deploy_service_list)
        if len(self.remain_deploy_service_list) > 0:
            h1("Step: Deploy ECS Service")
            for service in self.remain_deploy_service_list:
                self.task_queue.put([service, ProcessMode.deployService])
            self.task_queue.join()
            h2("Wait for Service Status 'Stable'")
            self._wait_for_stable(self.remain_deploy_service_list)

    def _check_deploy(self):
        h1("Step: Check Deploy ECS Service and Scheduled tasks")
        for service in self.all_deploy_target_service_list:
            self.task_queue.put([service, ProcessMode.checkDeployService])
        for scheduled_task in self.deploy_scheduled_task_list:
            self.task_queue.put([scheduled_task, ProcessMode.checkDeployScheduledTask])
        self.task_queue.join()

    def _wait_for_stable(self, service_list: list):
        if len(service_list) > 0:
            for service in service_list:
                self.task_queue.put([service, ProcessMode.waitForStable])
        self.task_queue.join()

    def _result_check(self):
        error_service_list = list(filter(
            lambda service: service.status == ProcessStatus.error, self.all_deploy_target_service_list
        ))
        error_scheduled_task_list = list(filter(
            lambda task: task.status == ProcessStatus.error, self.deploy_scheduled_task_list
        ))
        # エラーが一個でもあれば失敗としておく
        if len(error_service_list) > 0 or len(error_scheduled_task_list) > 0:
            sys.exit(1)
        if self.error:
            sys.exit(1)


def deregister_task_definition(awsutils, service: ecs.service.Service):
    if service.origin_task_definition_arn is None:
        return
    if service.is_same_task_definition():
        return
    awsutils.deregister_task_definition(service.origin_task_definition_arn)


def wait_for_stable(awsutils, service: ecs.service.Service, delay: int, max_attempts: int):
    try:
        res_service = awsutils.wait_for_stable(
            cluster_name=service.task_environment.cluster_name,
            service_name=service.service_name,
            max_attempts=max_attempts,
            delay=delay
        )
        service.update_run_count(describe_service=res_service, is_stop_before_deploy=False)
        deregister_task_definition(awsutils, service)
        success(
            "service '{service.service_name}' ({service.running_count:d} / {service.desired_count}) update completed."
            .format(service=service))
    except WaiterError:
        service.status = ProcessStatus.error
        error("service '{service.service_name}' update wait timeout.".format(service=service))


def test_templates(args):
    h1("Step: Check ECS Template")
    environment = None
    files = os.listdir(args.environment_yaml_dir)
    if files is None or len(files) == 0:
        raise Exception("environment yaml file not found.")
    services_config = yaml.load(args.services_yaml, Loader=yamlordereddictloader.Loader)
    for f in files:
        file_path = os.path.join(args.environment_yaml_dir, f)
        if os.path.isfile(file_path):
            with open(file_path, 'r') as environment_yaml:
                environment_config = yaml.load(environment_yaml.read(), Loader=yamlordereddictloader.Loader)

                environment = environment_config.get("environment")
                if environment is None:
                    raise VariableNotFoundException("%s requires parameter `environment`." % file_path)
                environment = render.render_template(
                    str(environment),
                    environment_config,
                    args.task_definition_config_env
                )

                ecs.service.get_service_list_yaml(
                    services_config=services_config,
                    environment_config=environment_config,
                    is_task_definition_config_env=args.task_definition_config_env,
                    environment=environment
                )
                get_scheduled_task_list(
                    services_config=services_config,
                    environment_config=environment_config,
                    is_task_definition_config_env=args.task_definition_config_env,
                    environment=environment
                )
        success("Template check environment `{environment}` done.".format(environment=environment))


def get_deploy_list(
        services_yaml,
        environment_yaml,
        task_definition_template_dir,
        task_definition_config_json,
        task_definition_config_env,
        deploy_service_group,
        template_group
):
    h1("Step: Check ECS Template")
    scheduled_task_list = []
    deploy_scheduled_task_list = []
    if services_yaml:
        services_config = yaml.load(services_yaml, Loader=yamlordereddictloader.Loader)
        environment_config = yaml.load(environment_yaml, Loader=yamlordereddictloader.Loader)

        environment = environment_config.get("environment")
        if environment is None:
            raise VariableNotFoundException("environment-yaml requires parameter `environment`.")
        environment = render.render_template(str(environment), environment_config, task_definition_config_env)

        service_list = ecs.service.get_service_list_yaml(
            services_config=services_config,
            environment_config=environment_config,
            is_task_definition_config_env=task_definition_config_env,
            environment=environment
        )

        scheduled_task_list = get_scheduled_task_list(
            services_config=services_config,
            environment_config=environment_config,
            is_task_definition_config_env=task_definition_config_env,
            environment=environment
        )
        deploy_scheduled_task_list = get_deploy_scheduled_task_list(
            scheduled_task_list, deploy_service_group, template_group)

    else:
        task_definition_config = json.load(task_definition_config_json)
        environment = task_definition_config['environment']
        service_list = ecs.service.get_service_list_json(
            task_definition_template_dir=task_definition_template_dir,
            task_definition_config=task_definition_config,
            task_definition_config_env=task_definition_config_env
        )
    deploy_service_list = ecs.service.get_deploy_service_list(service_list, deploy_service_group, template_group)

    # duplicate name check
    for deploy_service in deploy_service_list:
        for deploy_scheduled_task in deploy_scheduled_task_list:
            if deploy_service.family == deploy_scheduled_task.family:
                raise Exception('Duplicate family name `{family}` found.'.format(family=deploy_service.family))

    if len(deploy_service_list) == 0 and len(deploy_scheduled_task_list) == 0:
        error("Deployment target not found.")
        sys.exit(1)

    success("Template check environment `{environment}` done.".format(environment=environment))
    return service_list, deploy_service_list, scheduled_task_list, deploy_scheduled_task_list, environment
