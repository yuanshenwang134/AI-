"""Recover only jobs whose recorded local execution process has exited.

No age-based timeout: a long inference or Windows sleep is not proof of failure.
Legacy jobs without ownership remain explicitly unverified, never guessed dead.
"""
import json
import os
import socket
import sqlite3
import time
from contextlib import closing


def process_identity():
    try:
        import psutil
        return {'host':socket.gethostname(),'pid':os.getpid(),
                'started':psutil.Process().create_time()}
    except (ImportError, OSError):
        return None


def owner_alive(owner):
    if not isinstance(owner,dict) or owner.get('host') != socket.gethostname():
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        return abs(psutil.Process(int(owner['pid'])).create_time()-float(owner['started'])) < .01
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, KeyError, ValueError, TypeError, OSError):
        return None


def recover_jobs(path, alive=owner_alive):
    changed=[]
    with closing(sqlite3.connect(path)) as conn, conn:
        # Serialize startup recovery against completion from another live server.
        conn.execute('BEGIN IMMEDIATE')
        rows=conn.execute("SELECT job_id,payload FROM jobs WHERE status IN ('running','queued')").fetchall()
        for job_id,payload in rows:
            try:
                owner=json.loads(payload or '{}').get('_worker_owner')
            except (ValueError, AttributeError):
                owner=None
            state=alive(owner)
            if state is False:
                conn.execute("UPDATE jobs SET status='error', message=?, error=?, updated=? WHERE job_id=?",
                             ('任务已中断：执行进程已退出，请重新开始分析',
                              'worker_process_exited',time.time(),job_id))
                changed.append(job_id)
            elif not owner:
                conn.execute("UPDATE jobs SET message=? WHERE job_id=?",
                             ('历史任务状态待确认：缺少执行进程记录；请核对后重新分析',job_id))
    return changed
