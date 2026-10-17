#!/usr/bin/env python3
"""用一台临时 Debian 抢占实例把 Alpine 镜像从 CDN 搬到 OSS（本机上行太慢时用）。

流程: 建临时实例(带公网IP) -> ssh 进去 curl 下载 + 校验 -> 用预签名 PUT URL 走 OSS 内网上传 -> 释放临时实例。
AK/SK 不会进入临时实例，只有带签名的临时 URL。
"""
import argparse
import datetime
import hashlib
import subprocess
import sys
import time
from pathlib import Path

import oss2

import provision as P
from aliyun_common import ApiError, ecs, err_str, load_config, load_state, make_client, save_state

FILES = [  # (本地文件名, 下载 URL, 哈希算法)
    ("generic_alpine-3.22.5-x86_64-bios-cloudinit-r0.qcow2",
     "https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/cloud/generic_alpine-3.22.5-x86_64-bios-cloudinit-r0.qcow2", "sha512"),
    ("alpine-virt-3.22.6-x86_64.iso",
     "https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/x86_64/alpine-virt-3.22.6-x86_64.iso", "sha256"),
]


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def file_hash(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-dir", required=True, help="本地已下载好的镜像目录（用于取哈希）")
    ap.add_argument("--keep", action="store_true", help="完成后不释放临时实例")
    ap.add_argument("--cleanup", action="store_true", help="只释放 state 里记录的临时实例")
    args = ap.parse_args()

    defaults, accounts = load_config()
    key, acc = next(iter(accounts.items()))
    client, region = make_client(acc, defaults)
    st = load_state(acc)

    if args.cleanup:
        bid = st.get("bridge_instance_id")
        if bid:
            ecs(client, "DeleteInstance", InstanceId=bid, Force="true")
            log(f"已释放临时实例 {bid}")
            st.pop("bridge_instance_id", None)
            save_state(key, st)
        return

    bucket_name = st["oss_bucket"]
    auth = oss2.Auth(acc["access_key_id"], acc["access_key_secret"])
    pub = oss2.Bucket(auth, f"https://oss-{region}.aliyuncs.com", bucket_name, enable_crc=False, connect_timeout=120)
    internal = oss2.Bucket(auth, f"https://oss-{region}-internal.aliyuncs.com", bucket_name, enable_crc=False)

    jobs = []
    for name, url, algo in FILES:
        local = Path(args.local_dir) / name
        size = local.stat().st_size
        if pub.object_exists(name) and pub.head_object(name).content_length == size:
            log(f"OSS 已有 {name}，跳过")
            continue
        jobs.append((name, url, algo, file_hash(local, algo), size, internal.sign_url("PUT", name, 3600)))
    if not jobs:
        log("没有需要搬运的文件")
        return

    # 临时实例
    zone = st["zone"]
    vsw = st[f"vswitch_{zone}"]
    bid = st.get("bridge_instance_id")
    if not bid or not P.instance_info(client, region, bid):
        image = P.pick_public_image(client, region, "debian_12_")
        r = ecs(client, "RunInstances", RegionId=region, ZoneId=zone, ImageId=image, InstanceType="ecs.e-c2m1.large",
                SecurityGroupId=st["sg_id"], VSwitchId=vsw, InstanceName="cdt-bridge", HostName="cdt-bridge",
                KeyPairName=st["keypair"], InstanceChargeType="PostPaid", SpotStrategy="SpotAsPriceGo",
                InternetChargeType="PayByTraffic", InternetMaxBandwidthOut=100, Amount=1,
                ClientToken=f"cdt-bridge-{key}-{int(time.time())}",
                **{"SystemDisk.Category": "cloud_essd_entry", "SystemDisk.Size": 20})
        bid = r["InstanceIdSets"]["InstanceIdSet"][0]
        st["bridge_instance_id"] = bid
        save_state(key, st)
        log(f"创建临时实例 {bid}，等待运行...")
    P.wait(lambda: (P.instance_info(client, region, bid) or {}).get("Status"), "临时实例", {"Running"})
    ip = (P.instance_info(client, region, bid).get("PublicIpAddress", {}).get("IpAddress") or [None])[0]
    log(f"临时实例 {bid} 公网 IP {ip}")

    ssh = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
           "-o", "LogLevel=ERROR", f"root@{ip}"]
    for i in range(30):
        if subprocess.run(ssh + ["true"], capture_output=True).returncode == 0:
            break
        time.sleep(5)
    else:
        sys.exit("SSH 一直连不上临时实例")
    log("SSH 已连通，开始在实例内下载并上传")

    script = ["set -e", "cd /tmp"]
    for name, url, algo, digest, size, signed in jobs:
        script += [
            f'echo "== {name}"',
            f'curl -sSL --retry 3 -o "{name}" "{url}"',
            f'echo "{digest}  {name}" | {algo}sum -c -',
            f'curl -sS -X PUT -T "{name}" -H "Content-Type:" "{signed}" -o /dev/null -w "upload http=%{{http_code}}\\n"',
        ]
    res = subprocess.run(ssh + ["bash", "-s"], input="\n".join(script) + "\n", capture_output=True, text=True, timeout=900)
    print(res.stdout)
    if res.returncode != 0:
        print(res.stderr[-2000:])
        sys.exit(f"实例内执行失败，退出码 {res.returncode}")

    for name, url, algo, digest, size, signed in jobs:
        got = pub.head_object(name).content_length if pub.object_exists(name) else None
        log(f"OSS {name}: {got} bytes ({'OK' if got == size else '大小不符!'})")

    if not args.keep:
        ecs(client, "DeleteInstance", InstanceId=bid, Force="true")
        st.pop("bridge_instance_id", None)
        save_state(key, st)
        log(f"已释放临时实例 {bid}")


if __name__ == "__main__":
    main()
