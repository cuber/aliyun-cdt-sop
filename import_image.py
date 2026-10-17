#!/usr/bin/env python3
"""把本地镜像文件上传到香港 OSS，并导入为 ECS 自定义镜像（记录在 state/<name>.json 的 images 里）。

用法:
  python import_image.py --file generic_alpine-xxx.qcow2 --name alpine-cloud --format QCOW2 --size 1
  python import_image.py --file alpine-virt-xxx.iso --name alpine-iso --format RAW --size 1
  python import_image.py --status            # 查看导入进度
"""
import argparse
import datetime
import secrets
import sys
import time
from pathlib import Path

import oss2

from aliyun_common import ApiError, ecs, err_str, load_config, load_state, make_client, save_state

IMG_STATUS = "Creating,Waiting,Available,UnAvailable,CreateFailed,Deprecated"


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def image_status(client, region, image_id):
    lst = ecs(client, "DescribeImages", RegionId=region, ImageId=image_id, Status=IMG_STATUS)["Images"]["Image"]
    return (lst[0]["Status"], lst[0].get("Progress")) if lst else ("NotFound", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--name")
    ap.add_argument("--format", choices=["QCOW2", "RAW", "VHD"])
    ap.add_argument("--size", type=int, default=1, help="导入后系统盘大小 GiB，最小 1")
    ap.add_argument("--account")
    ap.add_argument("--wait", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--role-name", default="AliyunECSImageImportDefaultRole", help="ECS 读 OSS 用的角色名，默认即阿里云约定的名字；角色叫别的名字就在这里指定")
    args = ap.parse_args()

    defaults, accounts = load_config()
    for key, acc in accounts.items():
        if args.account and key != args.account:
            continue
        if not acc.get("access_key_id"):
            continue
        client, region = make_client(acc, defaults)
        st = load_state(acc)
        images = st.setdefault("images", {})
        print(f"\n#### 账号 {key} ({acc.get('name')}) region={region}")

        if args.status:
            for name, info in images.items():
                s, p = image_status(client, region, info["image_id"])
                print(f"  {name}: {info['image_id']} {s} {p or ''}")
            continue
        if not (args.file and args.name and args.format):
            sys.exit("需要 --file --name --format")

        bucket_name = st.get("oss_bucket") or f"cdt-img-{key}-{secrets.token_hex(3)}"
        auth = oss2.Auth(acc["access_key_id"], acc["access_key_secret"])
        bucket = oss2.Bucket(auth, f"https://oss-{region}.aliyuncs.com", bucket_name, enable_crc=False, connect_timeout=300)
        try:
            bucket.get_bucket_info()
        except oss2.exceptions.NoSuchBucket:
            bucket.create_bucket(oss2.BUCKET_ACL_PRIVATE)
            log(f"创建 OSS bucket {bucket_name} @ {region}")
        st["oss_bucket"] = bucket_name
        save_state(key, st)

        path = Path(args.file)
        obj = path.name
        if bucket.object_exists(obj) and bucket.head_object(obj).content_length == path.stat().st_size:
            log(f"OSS 已有 {obj}，跳过上传")
        else:
            log(f"上传 {obj} ({path.stat().st_size / 2 ** 20:.1f} MB) -> oss://{bucket_name}/{obj}")
            last = [0]
            def prog(done, total):
                pct = int(done * 100 / total)
                if pct >= last[0] + 10:
                    last[0] = pct
                    log(f"  上传进度 {pct}%")
            oss2.resumable_upload(bucket, obj, str(path), multipart_threshold=2 * 2 ** 20, part_size=1 * 2 ** 20, num_threads=2, progress_callback=prog)
            log("上传完成")

        if not images.get(args.name, {}).get("image_id"):
            lst = ecs(client, "DescribeImages", RegionId=region, ImageOwnerAlias="self", ImageName=args.name, Status=IMG_STATUS)["Images"]["Image"]
            lst = [i for i in lst if i.get("ImageName") == args.name]
            if lst:
                images[args.name] = {"image_id": lst[0]["ImageId"], "object": obj, "format": args.format, "size": args.size}
                save_state(key, st)
                log(f"按名称找到已导入的镜像 {args.name}: {lst[0]['ImageId']}")
        if images.get(args.name, {}).get("image_id"):
            iid = images[args.name]["image_id"]
            log(f"镜像 {args.name} 已有导入记录 {iid}")
        else:
            try:
                r = ecs(client, "ImportImage", RegionId=region, ImageName=args.name, Platform="Others Linux",
                        Architecture="x86_64", OSType="linux", BootMode="BIOS", RoleName=args.role_name,
                        Description=f"from oss://{bucket_name}/{obj}",
                        **{"DiskDeviceMapping.1.OSSBucket": bucket_name, "DiskDeviceMapping.1.OSSObject": obj,
                           "DiskDeviceMapping.1.Format": args.format, "DiskDeviceMapping.1.DiskImageSize": args.size})
            except ApiError as e:
                log(f"ImportImage 失败: {err_str(e)}")
                continue
            iid = r["ImageId"]
            images[args.name] = {"image_id": iid, "task_id": r.get("TaskId"), "object": obj, "format": args.format, "size": args.size}
            save_state(key, st)
            log(f"ImportImage 已提交: {iid} task={r.get('TaskId')}")

        if args.wait:
            while True:
                s, p = image_status(client, region, iid)
                log(f"镜像 {iid} {s} {p or ''}")
                if s in ("Available", "UnAvailable", "CreateFailed", "NotFound"):
                    break
                time.sleep(20)


if __name__ == "__main__":
    main()
