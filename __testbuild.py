#!/usr/bin/env python

import time, sys, inspect, traceback, argparse, json, uuid, requests
from pprint import pprint
from subprocess import Popen, PIPE

try:
    from termcolor import colored
except ImportError:
    sys.stderr.write('ERROR: Python module "termcolor" not found, please run "pip install termcolor".\n')
    sys.exit(1)

try:
    import requests
except ImportError:
    sys.stderr.write('ERROR: Python module "requests" not found, please run "pip install requests".\n')
    sys.exit(1)

PROXY_HOST = None
PROXY_PORT = None
BASE_API_URL = 'https://api.digitalocean.com/v2'
DOCKER_IMAGE_SLUG = 'docker'
DEFAULT_SSH_KEYS = ['d1:b6:92:ea:cc:4c:fe:9c:c5:ef:27:ce:33:1f:ba:61']
DEFAULT_REGION_SLUG = 'nyc3'
DEFAULT_MEMORY_SIZE_SLUG = '512mb'
DEFAULT_VCPUS = 1
DEFAULT_DISK_SIZE = 20

from functools import wraps

DEFAULT_TRIES = 4
DEFAULT_DELAY = 30
DEFAULT_BACKOFF = 2

def retry(ExceptionToCheck, tries=DEFAULT_TRIES, delay=DEFAULT_DELAY, backoff=DEFAULT_BACKOFF, cdata=None):
    '''Retry calling the decorated function using an exponential backoff.

    http://www.saltycrane.com/blog/2009/11/trying-out-retry-decorator-python/
    original from: http://wiki.python.org/moin/PythonDecoratorLibrary#Retry

    :param ExceptionToCheck: the exception to check. may be a tuple of
        exceptions to check
    :type ExceptionToCheck: Exception or tuple
    :param tries: number of times to try (not retry) before giving up
    :type tries: int
    :param delay: initial delay between retries in seconds
    :type delay: int
    :param backoff: backoff multiplier e.g. value of 2 will double the delay
        each retry
    :type backoff: int
    :param logger: logger to use. If None, print
    :type logger: logging.Logger instance
    '''
    def deco_retry(f):
        @wraps(f)
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 0:
                try:
                    return f(*args, **kwargs)
                except ExceptionToCheck, e:
                    logger(message='%s, retrying in %d seconds (mtries=%d): %s' % (repr(e), mdelay, mtries, str(cdata)))
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return f(*args, **kwargs)
        return f_retry  # true decorator
    return deco_retry

def logger(message=None):
    print '%s\n' % repr(message)
    
def args():
    parser = argparse.ArgumentParser()
    sp = parser.add_subparsers()    
    digitalocean = sp.add_parser('digitalocean')
    digitalocean.add_argument('provider', action='store_const', const='digitalocean', help=argparse.SUPPRESS)
    digitalocean.add_argument('--api_token', type=str, required=True, help='DigitalOcean API v2 secret token')
    args = parser.parse_args()
    return args

def create_droplet(s, name):
    user_data = '''
#cloud-config

runcmd:
  - git clone https://github.com/ab77/netflix-proxy /opt/netflix-proxy && cd /opt/netflix-proxy && ./build.sh -c 127.0.0.1
'''

    json_data = {'name': name,
                 'region': DEFAULT_REGION_SLUG,
                 'size': DEFAULT_MEMORY_SIZE_SLUG,
                 'vcpus': DEFAULT_VCPUS,
                 'disk': DEFAULT_DISK_SIZE,
                 'image': DOCKER_IMAGE_SLUG,
                 'ssh_keys': DEFAULT_SSH_KEYS,
                 'backups': False,
                 'ipv6': False,
                 'private_networking': False,
                 'user_data': user_data}
    
    s.headers.update({'Content-Type': 'application/json'})
    post_body = json.dumps(json_data)
    response = s.post('%s/droplets' % BASE_API_URL, data=post_body)
    d = json.loads(response.text)
    pprint(d)

    @retry(AssertionError, cdata='method=%s()' % inspect.stack()[0][3])
    def wait_for_vm_provisioning_completion_retry(action_url):
        response = s.get(action_url)
        d = json.loads(response.text)
        if 'completed' in d['action']['status']:
            print colored(d['action']['status'], 'green')
            assert True
            return d
        else:
            print colored(d['action']['status'], 'red')
            assert False
            return None
        
    if 'links' not in d:
        return False
    else:
        return wait_for_vm_provisioning_completion_retry(d['links']['actions'][0]['href'])

def destroy_droplet(s, droplet_id):

    @retry(AssertionError, cdata='method=%s()' % inspect.stack()[0][3])
    def wait_for_vm_deletion_completion_retry(s, droplet_id):
        response = s.delete('%s/droplets/%d' % (BASE_API_URL,
                                                droplet_id))
        if response.__dict__['status_code'] == 204:
            print colored(response.__dict__['status_code'], 'green')
            assert True
            return response.__dict__
        else:
            print colored(response.__dict__['status_code'], 'red')
            assert False
            return None

    return wait_for_vm_deletion_completion_retry(s, droplet_id)

def get_droplet_id_by_name(s, name):
    response = s.get('%s/droplets' % BASE_API_URL)
    d = json.loads(response.text)
    droplet_id = None
    for droplet in d['droplets']:
        if name in droplet['name']:
            droplet_id = droplet['id']
            
    return droplet_id

def get_droplet_ip_by_name(s, name):
    response = s.get('%s/droplets' % BASE_API_URL)
    d = json.loads(response.text)
    droplet_id = None
    for droplet in d['droplets']:
        if name in droplet['name']:
            droplet_ip = droplet['networks']['v4'][0]['ip_address']
            
    return droplet_ip

def ssh_run_command(ip, command):
    result = None
    ssh = Popen(['ssh', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'StrictHostKeyChecking=no',
                 '-i', 'id_rsa.travis', 'root@%s' % ip, command],
                shell=False, stdout=PIPE, stderr=PIPE)
    (stdout, stderr) = ssh.communicate()
    print colored('%s: pid = %d, stdout = %s, stderr = %s, rc = %d' % (inspect.stack()[0][3],
                                                                       ssh.pid,
                                                                       stdout.splitlines(),
                                                                       stderr.splitlines(),
                                                                       ssh.returncode), 'grey')
    return dict({'stdout': stdout.splitlines(),
                 'stderr': stderr.splitlines(),
                 'rc': ssh.returncode,
                 'pid': ssh.pid})

def docker_test(ip):

    @retry(AssertionError, cdata='method=%s()' % inspect.stack()[0][3])
    def docker_test_retry(ip):
        stdout = ssh_run_command(ip, 'docker ps')['stdout']
        if len(stdout) < 3: # quick and dirty check (3 lines of output = header + bind + sniproxy), needs improvement..
            print colored('%s: stdout = %s, len(stdout) = %d' % (inspect.stack()[0][3],
                                                                 stdout,
                                                                 len(stdout)), 'red')
            assert False
            return False
        else:
            print colored('%s: stdout = %s, len(stdout) = %d' % (inspect.stack()[0][3],
                                                                 stdout,
                                                                 len(stdout)), 'green')
            assert True
            return True
            
    return docker_test_retry(ip)

def netflix_proxy_test(ip):

    @retry(AssertionError, cdata='method=%s()' % inspect.stack()[0][3])
    def netflix_proxy_test_retry(ip):
        ssh_run_command(ip, 'tail /var/log/cloud-init-output.log')
        rc = ssh_run_command(ip, "grep -E 'Change your DNS to ([0-9]{1,3}[\.]){3}[0-9]{1,3} and start watching Netflix out of region\.' /var/log/cloud-init-output.log")['rc']
        if rc > 0:
            print colored('%s: SSH return code = %s' % (inspect.stack()[0][3], rc), 'red')
            assert False
            return None
        else:
            print colored('%s: SSH return code = %s' % (inspect.stack()[0][3], rc), 'green')
            assert True
            return rc
            
    return netflix_proxy_test_retry(ip)

if __name__ == '__main__':
    arg = args()
    if arg.api_token:
        name = str(uuid.uuid4())
        name = '6b51bf5e-bf12-4a42-ba40-9c6b1c29a99f'
        droplet_id = None
        s = requests.Session()
        if PROXY_HOST and PROXY_PORT:
            s.verify = False
            s.proxies = {'http' : 'http://%s:%s' % (PROXY_HOST, PROXY_PORT),
                         'https': 'https://%s:%s' % (PROXY_HOST, PROXY_PORT)}
        s.headers.update({'Authorization': 'Bearer %s' % arg.api_token})
        
        try:
            print colored('Creating Droplet %s...' % name, 'yellow')
            d = create_droplet(s, name)                
            pprint(d)
            
            droplet_ip = get_droplet_ip_by_name(s, name)
            print colored('Droplet ipaddr = %s...' % droplet_ip, 'cyan')

            print colored('Checking running Docker containers on Droplet with name = %s, ipaddr = %s...' % (name, droplet_ip), 'yellow')
            result = docker_test(droplet_ip)
            if not result: sys.exit(1)
            
            print colored('Testing netflix-proxy on Droplet with name = %s, ipaddr = %s...' % (name, droplet_ip), 'yellow')
            rc = netflix_proxy_test(droplet_ip)
            if rc > 0: sys.exit(rc)

            print colored('Tested, OK..', 'green')
            sys.exit(0)
            
        except Exception as e:
            print colored(traceback.print_exc(), 'red')
            sys.exit(1)
            
        finally:
            droplet_id = get_droplet_id_by_name(s, name)
            if droplet_id:
                print colored('Destroying Droplet name = %s, id = %s...' % (name, droplet_id), 'yellow')
                d = destroy_droplet(s, droplet_id)
                pprint(d)
