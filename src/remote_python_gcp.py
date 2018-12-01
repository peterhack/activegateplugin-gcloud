import datetime
import glob
import json
import logging
import os
import re

from ruxit.api.base_plugin import RemoteBasePlugin
from googleapiclient.discovery import build
from google.oauth2 import service_account


COMPUTE_METRICS = [
    'compute.googleapis.com/instance/cpu/utilization',
    'compute.googleapis.com/instance/disk/read_bytes_count',
    'compute.googleapis.com/instance/disk/read_ops_count',
    'compute.googleapis.com/instance/disk/write_bytes_count',
    'compute.googleapis.com/instance/disk/write_ops_count',
    'compute.googleapis.com/instance/network/received_bytes_count',
    'compute.googleapis.com/instance/network/received_packets_count',
    'compute.googleapis.com/instance/network/sent_bytes_count',
    'compute.googleapis.com/instance/network/sent_packets_count',
    'compute.googleapis.com/instance/uptime'
]

#class RemoteGCPPlugin():
class RemoteGCPPlugin(RemoteBasePlugin):
    logger = logging.getLogger(__name__)

    def initialize(self, **kwargs):
        self.debug = False
        self.project_jwt = kwargs['config']['project_jwt']

        # json.loads encapsulates keys and value in ' - the google library needs it encapsulated in "
        self.project_jwt = str(self.project_jwt).replace('{\'', '{"')
        self.project_jwt = str(self.project_jwt).replace('\': \'', '": "')
        self.project_jwt = str(self.project_jwt).replace('\', \'', '", "')
        self.project_jwt = str(self.project_jwt).replace('\'}', '"}')

        if 'debug' in kwargs:
            self.debug = True

        return

    def log(self, message):
        if self.debug:
            print(message)
        else:
            self.logger.info(__name__ + ": " + message)

    @staticmethod
    def format_rfc3339(datetime_instance):
        return datetime_instance.isoformat("T") + "Z"

    def get_start_time(self):
        start_time = (datetime.datetime.utcnow() -
                      datetime.timedelta(minutes=5))
        return self.format_rfc3339(start_time)

    def get_end_time(self):
        end_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=0)
        return self.format_rfc3339(end_time)

    def build_requests(self, client, project_id):
        requests = []
        project_resource = "projects/{}".format(project_id)

        for metric in COMPUTE_METRICS:
            request = client.projects().timeSeries().list(
                name=project_resource,
                filter='metric.type="{}"'.format(metric),
                interval_startTime=self.get_start_time(),
                interval_endTime=self.get_end_time())
            requests.append(request)

        return requests

    @staticmethod
    def execute_requests(requests):
        responses = []

        for request in requests:
            response = request.execute()
            responses.append(response)

        return responses

    @staticmethod
    def get_or_create_instance(instances, instance_name):
        for instance in instances:
            if instance['instance_name'] == instance_name:
                return instance

        instance = {'instance_name': instance_name}
        instances.append(instance)

        return instance

    def process_response(self, instances, response):
        # check if there was a data point in the given time frame
        if 'timeSeries' in response:
            for time_series in response['timeSeries']:
                instance_name = time_series['metric']['labels']['instance_name']
                instance = self.get_or_create_instance(instances, instance_name)

                instance['instance_id'] = time_series['resource']['labels']['instance_id']
                instance['zone'] = time_series['resource']['labels']['zone']

                if not 'metrics' in instance:
                    instance['metrics'] = {}

                # replace '/' with '-' because the remote plugin doesn't allow metrics with '/' in the name
                metric_name = str(time_series['metric']['type']).replace('/', '-')
                metric_value_type = str(time_series['valueType']).lower() + "Value"

                # TODO: instead of taking first value, calculate avg of returned metric points (sliding average)
                # TODO: review statistical background and recommendation 
                metric_value = time_series['points'][0]['value'][metric_value_type]
                metric_timestamp = time_series['points'][0]['interval']['endTime']

                instance['metrics'][metric_name] = {}
                instance['metrics'][metric_name]['valueType'] = metric_value_type
                instance['metrics'][metric_name]['value'] = metric_value
                instance['metrics'][metric_name]['timestamp'] = metric_timestamp

    def get_instances(self, project_jwt):
        instances = []
        project_id = project_jwt["project_id"]

        self.log("Processing '{}'".format(project_id))

        #credentials = service_account.Credentials.from_service_account_file(project_jwt)
        credentials = service_account.Credentials.from_service_account_info(project_jwt)
        client = build('monitoring', 'v3', credentials=credentials)

        requests = self.build_requests(client, project_id)
        responses = self.execute_requests(requests)

        for response in responses:
            self.process_response(instances, response)

        return instances

    def get_instance_details(self, list_of_instances, project_jwt):
        self.log("Getting instance details")

        credentials = service_account.Credentials.from_service_account_info(project_jwt)
        compute = build('compute', 'v1', credentials=credentials)

        project_id = project_jwt["project_id"]

        for instance in list_of_instances:
            zone = instance['zone']
            instance_name = instance['instance_name']

            request = compute.instances().get(project=project_id, zone=zone, instance=instance_name)
            instance_details = request.execute()

            instance['properties'] = {}
            instance['properties']['creation_timestamp'] = instance_details['creationTimestamp']
            instance['properties']['description'] = instance_details['description']
            instance['properties']['machineType'] = instance_details['machineType']
            instance['properties']['status'] = instance_details['status']
            instance['properties']['cpuPlatform'] = instance_details['cpuPlatform']
            instance['properties']['link'] = instance_details['selfLink']

            nic_counter = 0
            for instance_nic in instance_details['networkInterfaces']:
                key = 'nic_' + str(nic_counter) + '_name'
                instance['properties'][key] = instance_nic['name']
                key = 'nic_' + str(nic_counter) + '_ip'
                instance['properties'][key] = instance_nic['networkIP']

                access_config_counter = 0
                for accessConfig in instance_nic['accessConfigs']:
                    key = 'nic_' + str(nic_counter) + '_accessConfig_' + str(access_config_counter) + '_ip'
                    instance['properties'][key] = accessConfig['natIP']

                    access_config_counter = access_config_counter + 1

                nic_counter = nic_counter + 1

        return list_of_instances

    def create_group(self, group_id, group_name, project_id):
        extended_group_id = group_id + "_" + project_id
        extended_group_name = group_name + " (" + project_id + ")"

        self.log("- group id='{}', name='{}'".format(extended_group_id, extended_group_name))

        if self.debug:
            group = None
        else:
            group = self.topology_builder.create_group(extended_group_id, extended_group_name)

        return group

    def report_metrics(self, group, list_of_instances):
        for instance in list_of_instances:
            self.log("--- element '{}', '{}'".format(instance['instance_id'], instance['instance_name']))

            if not self.debug:
                element = group.create_element(instance['instance_id'], instance['instance_name'])

            for key,value in instance['properties'].items():
                self.log("---- element_property {}={}".format(key, value))

                # consider endpoints to add to element
                containsIpAddress = re.match(".*_ip$", key)

                if containsIpAddress:
                    self.log("---- endpoint {}".format(value))

                    if not self.debug:
                        element.add_endpoint(value, 0)

                if not self.debug:
                    element.report_property(key, value)

            for key, value in instance['metrics'].items():
                self.log("----- absolute {}={}".format(key, value['value']))

                if not self.debug:
                    element.absolute(key=key, value=value['value'])

    def query(self, **kwargs):
        project_jwt_json = json.loads(self.project_jwt)

        list_of_instances = self.get_instances(project_jwt_json)
        list_of_instances = self.get_instance_details(list_of_instances, project_jwt_json)

        group = self.create_group("ComputeEngine", "Compute Engine", project_jwt_json["project_id"])

        self.report_metrics(group, list_of_instances)

#class Test:
#    @staticmethod
#    def test():
#        plugin = RemoteGCPPlugin()
#
#        with open('gcp-project.json') as project_jwt_json:
#            project_jwt = json.load(project_jwt_json)
#            kwargs = {'debug': True, 'config': { 'project_jwt' : str(project_jwt)}}
#
#        plugin.initialize(**kwargs)
#        plugin.query()
#
#if __name__ == "__main__":
#    Test.test()
