"""Bounded, read-only Linux host metrics. No process/account details are exposed."""
import copy
import csv
import io
import math
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time


def cpu_counters(text):
    line=next(line for line in text.splitlines() if line.startswith('cpu '))
    # guest and guest_nice are already counted in user/nice; exclude them.
    ticks=[int(value) for value in line.split()[1:9]]
    if len(ticks)<5:
        raise ValueError('Incomplete CPU counters')
    return sum(ticks),ticks[3]+ticks[4]


def cpu_percent(previous,current):
    if previous is None:
        return None
    total=current[0]-previous[0]
    idle=current[1]-previous[1]
    if total<=0 or idle<0 or idle>total:
        return None
    return round(100*(total-idle)/total,1)


def memory_usage(text):
    values={line.split(':',1)[0]:int(line.split()[1])*1024
            for line in text.splitlines() if ':' in line and len(line.split())>=2}
    total,available=values.get('MemTotal'),values.get('MemAvailable')
    if not total or available is None or not 0<=available<=total:
        return None
    used=total-available
    return {'percent':round(used/total*100,1),'usedBytes':used,'totalBytes':total}


def numeric(value):
    try:
        result=float(value.strip())
        return result if math.isfinite(result) and result>=0 else None
    except ValueError:
        return None


def gpu_usage(text):
    result=[]
    for row in csv.reader(io.StringIO(text)):
        if len(row)!=5:
            continue
        index,name,utilization,used,total=[field.strip() for field in row]
        percent=numeric(utilization)
        used,total=numeric(used),numeric(total)
        result.append({'index':index,'name':name,'percent':percent if percent is not None and percent<=100 else None,
                       'usedBytes':used*1024**2 if used is not None else None,
                       'totalBytes':total*1024**2 if total is not None else None})
    return result


class SystemMonitor:
    def __init__(self):
        self.lock=threading.Lock()
        self.previous=None
        self.gpu_command=shutil.which('nvidia-smi')
        self.state={'hostname':socket.gethostname(),'cpu':None,'memory':None,'gpus':[],
                    'gpuStatus':'loading','updatedAt':None,'interval':2}

    def sample(self):
        cpu=memory=None
        try:
            current=cpu_counters(Path('/proc/stat').read_text())
            cpu=cpu_percent(self.previous,current)
            self.previous=current
        except (OSError,ValueError,StopIteration):
            self.previous=None
        try:
            memory=memory_usage(Path('/proc/meminfo').read_text())
        except (OSError,ValueError):
            pass
        gpus=[]
        gpu_status='unavailable'
        if self.gpu_command:
            try:
                query=subprocess.run([self.gpu_command,'--query-gpu=index,name,utilization.gpu,memory.used,memory.total',
                                      '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=1.5,check=True)
                gpus=gpu_usage(query.stdout)
                gpu_status='ready' if gpus else 'unavailable'
            except (OSError,subprocess.SubprocessError):
                gpu_status='error'
        with self.lock:
            self.state.update(cpu=cpu,memory=memory,gpus=gpus,gpuStatus=gpu_status,updatedAt=time.time())

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def run(self,stop):
        while not stop.is_set():
            start=time.monotonic()
            self.sample()
            stop.wait(max(.1,2-(time.monotonic()-start)))
