#!/usr/bin/env python

import datetime
import base64
import urllib.request
import json
import traceback
import sys
import time
import logging
import os
import socket
import prometheus_client
import prometheus_client.core

# add prometheus decorators
REQUEST_TIME = prometheus_client.Summary('request_processing_seconds', 'Time spent processing request')

def get_config():
    '''Get configuration from ENV variables'''
    conf['name'] = 'alertmanager'
    conf['tasks'] = ['metrics']
    conf['labels_and_annotations_to_get'] = 'alertname severity host_priority priority message summary'.split()
    conf['keys_to_get'] = list()
    env_lists_options = ['tasks', 'labels_and_annotations_to_get', 'keys_to_get']
    for opt in env_lists_options:
        opt_val = os.environ.get(opt.upper())
        if opt_val:
            conf[opt] = opt_val.split()
    conf['url'] = 'http://alertmanager:9093/api/v1/alerts'
    conf['log_level'] = 'INFO'
    conf['header_user_agent'] = ''
    conf['test'] = ''
    env_text_options = ['url', 'log_level', 'header_user_agent', 'test']
    for opt in env_text_options:
        opt_val = os.environ.get(opt.upper())
        if opt_val:
            conf[opt] = opt_val
    conf['check_timeout'] = 10
    conf['main_loop_sleep_interval'] = 10
    conf['listen_port'] = 9753
    env_int_options = ['check_timeout', 'main_loop_sleep_interval', 'listen_port']
    for opt in env_int_options:
        opt_val = os.environ.get(opt.upper())
        if opt_val:
            conf[opt] = int(opt_val)
    conf['errors_map'] = [
        {
            'name': 'status',
            'error': 'No "status" key in metrics json',
            'value': 1,
        },
        {
            'name': 'status',
            'expect': 'success',
            'error': '"status" key is not "success"',
            'value': 2,
        },
        {
            'name': 'data',
            'error': 'No "data" key in metrics json',
            'value': 3,
        },
        {
            'name': 'default_exporter_error',
            'error': 'none',
            'value': 0,
        },
    ]
    conf['alerts_status_map'] = {
        'active': 1,
        'unprocessed': 0,
        'suppressed': -1,
    }

def configure_logging():
    '''Configure logging module'''
    log = logging.getLogger(__name__)
    log.setLevel(conf['log_level'])
    FORMAT = '%(asctime)s %(levelname)s %(message)s'
    logging.basicConfig(format=FORMAT)
    return log

# Decorate function with metric.
@REQUEST_TIME.time()
def get_data():
    '''Get data from target service'''
    for task_name in conf['tasks']:
        get_data_function = globals()['get_data_'+ task_name]
        get_data_function()
                
def get_data_metrics():
    '''Get metrics via http'''
    if 'json' in conf['test']:
        file_name = '/opt/exporter/test/{}'.format(conf['test'])
        log.debug('Test mode, parsing metrics from file: "{}"'.format(file_name))
        with open(file_name) as response_file:
            json_data = json.load(response_file)
            parse_data_metrics(json_data)
            return
    url = conf['url']
    req = urllib.request.Request(url)
    if conf['header_user_agent']:
        req.add_header('User-Agent', conf['header_user_agent'].encode())
    try:
        response = urllib.request.urlopen(req, timeout=conf['check_timeout'])
        alertmanager_exporter_http_code.set(response.getcode())
    except socket.timeout:
        alertmanager_exporter_http_code.set(-1)
    except urllib.error.HTTPError as e:
        alertmanager_exporter_http_code.set(e.code)
    raw_data = response.read().decode()
    json_data = json.loads(raw_data)
    log.debug('Parsing metrics from url: "{0}", data: "{1}"'.format(url, json_data))
    parse_data_metrics(json_data)

def parse_data_metrics(json_data):
    '''Parse data from get_data_metrics'''
    # get last error
    labels = dict()
    metric_name = '{}_exporter_last_metrics_parsing_error'.format(conf['name'])
    description = 'Last metrics parsing error if have any, label "none" = OK, value "0" = OK'
    for index, error in enumerate(conf['errors_map']):
        # error if key not in json
        if error['name'] not in json_data:
            labels['error'] = conf['errors_map'][index]['error']
            value = conf['errors_map'][index]['value']
            break
        # error if value for expected key not match
        elif 'expect' in error and json_data[error['name']] != error['expect']:
            labels['error'] = conf['errors_map'][index]['error']
            value = conf['errors_map'][index]['value']
            break
    metric = {'metric_name': metric_name, 'labels': labels, 'description': description, 'value': value}
    data.append(metric)
    if labels['error'] != 'none':
        return
    # get alerts stats
    for alert in json_data['data']:
        log.debug('parse_data_metrics, alert: "{}"'.format(alert))
        labels = dict()
        for key in conf['keys_to_get']:
            if key in alert:
                labels[key] = alert[key]
        for label in conf['labels_and_annotations_to_get']:
            if label in alert['labels']:
                labels[label] = alert['labels'][label]
            elif label in alert['annotations']:
                labels[label] = alert['annotations'][label]
        metric_name = '{}_exporter_alert'.format(conf['name'])
        description = 'Alertmanager alerts, "active"=1, "unprocessed"=0, "suppressed"=-1'
        alert_status = alert['status']['state']
        value = conf['alerts_status_map'][alert_status]
        metric = {'metric_name': metric_name, 'labels': labels, 'description': description, 'value': value}
        data.append(metric)

def label_clean(label):
    label = str(label)
    replace_map = {
        '\\': '',
        '"': '',
        '\n': '',
        '\t': '',
        '\r': '',
        '-': '_',
        ' ': '_'
    }
    for r in replace_map:
        label = label.replace(r, replace_map[r])
    return label

# run
conf = dict()
get_config()
log = configure_logging()
log.debug('Config: "{}"'.format(conf))
data_tmp = dict()
data = list()

alertmanager_exporter_up = prometheus_client.Gauge('alertmanager_exporter_up', 'exporter scrape status')
alertmanager_exporter_errors_total = prometheus_client.Counter('alertmanager_exporter_errors_total', 'exporter scrape errors total counter')
alertmanager_exporter_http_code = prometheus_client.Gauge('alertmanager_exporter_http_code', 'exporter scrape http code')

class Collector(object):
    def collect(self):
        # add static metrics
        gauge = prometheus_client.core.GaugeMetricFamily
        counter = prometheus_client.core.CounterMetricFamily
        # get dinamic data
        try:
            get_data()
            alertmanager_exporter_up.set(1)
        except:
            trace = traceback.format_exception(sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2])
            for line in trace:
                print(line[:-1], flush=True)
            alertmanager_exporter_up.set(0)
            alertmanager_exporter_errors_total.inc()
        # add dinamic metrics
        to_yield = set()
        for _ in range(len(data)):
            metric = data.pop()
            labels = list(metric['labels'].keys())
            labels_values = [ metric['labels'][k] for k in labels ]
            if metric['metric_name'] not in to_yield:
                setattr(self, metric['metric_name'], gauge(metric['metric_name'], metric['description'], labels=labels))
            if labels:
                getattr(self, metric['metric_name']).add_metric(labels_values, metric['value'])
                to_yield.add(metric['metric_name'])
        for metric in to_yield:
            yield getattr(self, metric)

registry = prometheus_client.core.REGISTRY
registry.register(Collector())

prometheus_client.start_http_server(conf['listen_port'])

# endless loop
while True:
    try:
        while True:
            time.sleep(conf['main_loop_sleep_interval'])
    except KeyboardInterrupt:
        break
    except:
        trace = traceback.format_exception(sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2])
        for line in trace:
            print(line[:-1], flush=True)

