#!/usr/bin/env python3
"""保活：按 CDT 本月已用流量，自动启停每个账号的实例（实例按 state 或实例名 cdt-hk 找，不用写在配置里）。

  流量 < 阈值  -> 实例若是 Stopped 则启动
  流量 >= 阈值 -> 实例若是 Running 则停机（节省停机模式，停机期间不收实例费）
每月 1 号 CDT 流量归零后会自动再启动。

用法:
  python keepalive.py            # 执行一次（配合 crontab 每分钟跑）
  python keepalive.py --loop     # 常驻，按 check_interval_seconds 轮询（默认 60 秒）
  python keepalive.py --dry-run  # 只打印判断，不启停

crontab 示例（在另一台机器上，不要放在被保活的实例里）:
  * * * * * cd /path/to/aliyun && .venv/bin/python keepalive.py >> keepalive.log 2>&1
"""
import argparse
import logging
import sys
import time

from aliyun_common import ApiError, call, ecs, err_str, load_config, load_state, make_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s", stream=sys.stdout)


def cdt_total_gb(client):
    r = call(client, "cdt.aliyuncs.com", "2021-08-13", "ListCdtInternetTraffic")
    return sum(d.get("Traffic", 0) for d in r.get("TrafficDetails", [])) / 1024 ** 3


def target_instances(key, acc, client=None, region=None):
    ids = [i for i in (acc.get("instances") or []) if i.get("instance_id") and not i["instance_id"].startswith("i-xxx")]
    if not ids:
        st = load_state(acc)
        if st.get("instance_id"):
            ids = [{"instance_id": st["instance_id"]}]
    if not ids and client:
        lst = ecs(client, "DescribeInstances", RegionId=region, InstanceName="cdt-hk", PageSize=20)["Instances"]["Instance"]
        ids = [{"instance_id": i["InstanceId"]} for i in lst if i.get("InstanceName") == "cdt-hk"]
    return ids


def run_once(dry_run=False):
    defaults, accounts = load_config()
    default_thr = defaults.get("traffic_threshold_gb") or 180
    for key, acc in accounts.items():
        if not acc.get("access_key_id"):
            continue
        log = logging.getLogger(acc.get("name") or key)
        client, region = make_client(acc, defaults)
        try:
            used_gb = cdt_total_gb(client)
        except ApiError as e:
            log.error("CDT 流量查询失败，跳过本轮: %s", err_str(e))
            continue
        insts = target_instances(key, acc, client, region)
        if not insts:
            log.warning("没有可保活的实例：state/ 里没有记录，按实例名 cdt-hk 也没找到，先跑 provision.py")
            continue
        for inst in insts:
            iid = inst["instance_id"]
            thr = inst.get("traffic_threshold_gb") or default_thr
            try:
                lst = ecs(client, "DescribeInstances", RegionId=region, InstanceIds=[iid])["Instances"]["Instance"]
            except ApiError as e:
                log.error("查询实例 %s 失败: %s", iid, err_str(e))
                continue
            if not lst:
                log.error("实例 %s 不存在，可能已被释放（抢占式被回收且未设置停机保留）", iid)
                continue
            status = lst[0]["Status"]
            want_running = used_gb < thr
            log.info("CDT 已用 %.2f / %d GB  实例 %s 状态 %s  期望 %s", used_gb, thr, iid, status,
                     "Running" if want_running else "Stopped")
            if dry_run:
                continue
            try:
                if want_running and status == "Stopped":
                    ecs(client, "StartInstances", RegionId=region, InstanceIds=[iid])
                    log.info("已发送启动指令 %s", iid)
                elif not want_running and status == "Running":
                    try:
                        ecs(client, "StopInstances", RegionId=region, InstanceIds=[iid], StoppedMode="StopCharging")
                        log.info("流量达阈值，已发送停机指令 %s（节省停机）", iid)
                    except ApiError as e:
                        if "StoppedMode" not in err_str(e):
                            raise
                        ecs(client, "StopInstances", RegionId=region, InstanceIds=[iid])
                        log.info("流量达阈值，已发送停机指令 %s（不支持节省停机，普通停机）", iid)
            except ApiError as e:
                log.error("启停 %s 失败: %s", iid, err_str(e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.loop:
        run_once(args.dry_run)
        return
    defaults, _ = load_config()
    interval = int(defaults.get("check_interval_seconds") or 60)
    while True:
        try:
            run_once(args.dry_run)
        except Exception as e:  # 常驻模式下不让单次异常退出
            logging.getLogger("loop").exception("本轮异常: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
