# coding: utf-8
import argparse
import logging
import sys

from ecs.deploy import DeployManager, test_templates

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format='%(levelname)s: %(message)s')
logging.getLogger("botocore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# Arguments parsing
def init():
    parser = argparse.ArgumentParser(description='Deploy Service on ECS')
    subparser = parser.add_subparsers(dest='command')
    subparser.required = True

    service_parser = subparser.add_parser("service")
    service_parser.add_argument('--key', default="")
    service_parser.add_argument('--secret', default="")
    service_parser.add_argument('--region', default='us-east-1')
    service_parser.add_argument('--task-definition-template-dir')
    service_parser.add_argument('--task-definition-config-json', type=argparse.FileType('r'))
    service_parser.add_argument('--services-yaml', type=argparse.FileType('r'))
    service_parser.add_argument('--environment-yaml', type=argparse.FileType('r'))
    service_parser.add_argument('-t', '--test', default=False, action='store_true')
    service_parser.add_argument('--dry-run', default=False, action='store_true')

    service_parser.add_argument('--task-definition-config-env', default=True, action='store_true')
    service_parser.add_argument('--no-task-definition-config-env', dest='task_definition_config_env', default=True,
                                action='store_false')
    service_parser.add_argument('--threads-count', type=int, default=10)
    service_parser.add_argument('--service-wait-max-attempts', type=int, default=180)
    service_parser.add_argument('--service-wait-delay', type=int, default=5)
    service_parser.add_argument('--service-zero-keep', dest='service_zero_keep', default=True, action='store_true')
    service_parser.add_argument('--no-service-zero-keep', dest='service_zero_keep', default=True, action='store_false')
    service_parser.add_argument('--stop-before-deploy', dest='stop_before_deploy', default=True, action='store_true')
    service_parser.add_argument('--no-stop-before-deploy', dest='stop_before_deploy',
                                default=True, action='store_false')
    service_parser.add_argument('--template-group')
    service_parser.add_argument('--deploy-service-group')
    service_parser.add_argument('--delete-unused-service', dest='delete_unused_service', default=True,
                                action='store_true')
    service_parser.add_argument('--no-delete-unused-service', dest='delete_unused_service', default=True,
                                action='store_false')
    service_parser.add_argument('--service-update-only', dest='service_update_only', default=False, action='store_true')
    service_parser.add_argument('--task-definition-update-only', dest='task_definition_update_only', default=False, action='store_true')

    test_templates_parser = subparser.add_parser("test-templates")
    test_templates_parser.add_argument('--task-definition-template-dir')
    test_templates_parser.add_argument('--task-definition-config-json')
    test_templates_parser.add_argument('--services-yaml', type=argparse.FileType('r'))
    test_templates_parser.add_argument('--environment-yaml-dir')
    test_templates_parser.add_argument('--task-definition-config-env', default=True, action='store_true')
    test_templates_parser.add_argument('--no-task-definition-config-env', dest='task_definition_config_env',
                                       default=True, action='store_false')

    delete_parser = subparser.add_parser("delete")
    delete_parser.add_argument('--environment', required=True)
    delete_parser.add_argument('--key', default="")
    delete_parser.add_argument('--secret', default="")
    delete_parser.add_argument('--region', default='us-east-1')
    delete_parser.add_argument('--threads-count', type=int, default=3)
    delete_parser.add_argument('--service-wait-max-attempts', type=int, default=72)
    delete_parser.add_argument('--service-wait-delay', type=int, default=5)
    delete_parser.add_argument('--force', action='store_true', default=False)

    argp = parser.parse_args()
    if argp.command == 'service':
        if argp.task_definition_update_only and argp.service_update_only:
            logger.error("Both --service-update-only and --task-definition-update-only cannnot be set.")
            sys.exit(1)
    return argp

if __name__ == '__main__':
    args = init()
    if args.command == 'test-templates':
        test_templates(args=args)
    else:
        service_manager = DeployManager(args)
        if args.command == 'delete':
            service_manager.delete()
        elif args.command == 'service':
            if args.test:
                logger.info("test is successful.")
            elif args.dry_run:
                service_manager.dry_run()
            else:
                service_manager.run()
