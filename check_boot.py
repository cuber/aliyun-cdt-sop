#!/usr/bin/env python3
"""查看实例的串口输出和屏幕截图，用于确认自定义镜像是否正常启动、cloud-init 是否识别到阿里云数据源。

用法: python check_boot.py [--account 别名] [--instance i-xxx] [--out 目录]
"""
import argparse
import base64
import datetime
from pathlib import Path

from aliyun_common import ApiError, ecs, err_str, load_config, load_state, make_client


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account")
    ap.add_argument("--instance")
    ap.add_argument("--out", default="state")
    ap.add_argument("--tail", type=int, default=80, help="串口输出只显示最后 N 行")
    args = ap.parse_args()
    defaults, accounts = load_config()
    for key, acc in accounts.items():
        if args.account and key != args.account:
            continue
        if not acc.get("access_key_id"):
            continue
        client, region = make_client(acc, defaults)
        iid = args.instance or load_state(acc).get("instance_id")
        if not iid:
            print(f"账号 {key}: 没有实例")
            continue
        lst = ecs(client, "DescribeInstances", RegionId=region, InstanceIds=[iid])["Instances"]["Instance"]
        if not lst:
            print(f"账号 {key}: 实例 {iid} 不存在")
            continue
        i = lst[0]
        priv = (i.get("VpcAttributes", {}).get("PrivateIpAddress", {}).get("IpAddress") or ["?"])[0]
        eip = i.get("EipAddress", {}).get("IpAddress")
        print(f"#### {iid} 状态={i['Status']} 内网={priv} EIP={eip} 规格={i.get('InstanceType')} 镜像={i.get('ImageId')}")
        try:
            r = ecs(client, "GetInstanceConsoleOutput", InstanceId=iid, RemoveSymbols="true")
            text = base64.b64decode(r.get("ConsoleOutput", "")).decode("utf-8", "replace")
            lines = text.splitlines()
            print(f"---- 串口输出（共 {len(lines)} 行，最后 {args.tail} 行）----")
            print("\n".join(lines[-args.tail:]))
            Path(args.out, f"console_{iid}.txt").write_text(text)
        except ApiError as e:
            print("串口输出获取失败:", err_str(e))
        try:
            r = ecs(client, "GetInstanceScreenshot", InstanceId=iid, WakeUp="true")
            p = Path(args.out, f"screen_{iid}_{datetime.datetime.now():%H%M%S}.jpg")
            p.write_bytes(base64.b64decode(r["Screenshot"]))
            print(f"---- 截图已保存 {p}")
        except ApiError as e:
            print("截图获取失败:", err_str(e))


if __name__ == "__main__":
    main()
