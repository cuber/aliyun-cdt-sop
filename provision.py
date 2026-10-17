#!/usr/bin/env python3
"""按帖子思路在香港创建：VPC/交换机/安全组 -> 密钥对 -> 抢占式实例(节省停机) -> EIP(按流量) -> 共享带宽(可选，--no-cbwp 跳过)。

幂等：已创建的资源记录在 state/<name>.json 并按名称复用，重复执行不会重复建。
缺 EIP/共享带宽权限时会先把实例建好，补权限后重跑即可补齐。

镜像选择（--image）：
  alpine-cloud  state 里 import_image.py 导入的 Alpine cloud-init 镜像，1GiB ESSD PL0（默认，若已导入）
  alpine-iso    帖子的 Alpine ISO 镜像，1GiB ESSD PL0，需要你 VNC 手工 setup-alpine
  debian        官方 Debian 12 公共镜像，20GiB ESSD Entry
  m-xxxx        直接给镜像 ID

用法:
  python provision.py --plan
  python provision.py [--image alpine-cloud] [--type ecs.e-c4m1.large] [--zone cn-hongkong-b]
"""
import argparse
import datetime
import re
import sys
import time
from pathlib import Path

from aliyun_common import ApiError, call, ecs, err_str, load_config, load_state, make_client, save_state, vpc

NAME = "cdt-hk"
DEFAULT_TYPE = "ecs.e-c4m1.large"
VPC_CIDR = "172.16.0.0/16"
IMG_STATUS = "Creating,Waiting,Available,UnAvailable,CreateFailed"


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------- 选型 ----------
def pick_zone(client, region, itype, zone):
    start = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    zones = [zone] if zone else [z["ZoneId"] for z in ecs(client, "DescribeZones", RegionId=region)["Zones"]["Zone"]]
    best = None
    for z in zones:
        try:
            r = ecs(client, "DescribeSpotPriceHistory", RegionId=region, ZoneId=z, InstanceType=itype,
                    NetworkType="vpc", IoOptimized="optimized", StartTime=start)
            hist = r.get("SpotPrices", {}).get("SpotPriceType", [])
            if hist and (best is None or hist[-1]["SpotPrice"] < best[1]):
                best = (z, hist[-1]["SpotPrice"], hist[-1]["OriginPrice"])
        except ApiError:
            pass
    if not best:
        sys.exit(f"{itype} 在 {region} 没有抢占式库存/价格")
    return best


def pick_public_image(client, region, prefix="debian_12_"):
    imgs, page = [], 1
    while True:
        r = ecs(client, "DescribeImages", RegionId=region, ImageOwnerAlias="system", OSType="linux",
                Architecture="x86_64", PageSize=100, PageNumber=page)
        batch = r.get("Images", {}).get("Image", [])
        imgs += batch
        if len(batch) < 100:
            break
        page += 1
    cand = [i for i in imgs if i["ImageId"].startswith(prefix) and "uefi" not in i["ImageId"]]

    def ver(i):
        m = re.match(r"[a-z]+_(\d+)_(\d+)_", i["ImageId"])
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    cand.sort(key=lambda i: (ver(i), i.get("CreationTime", "")), reverse=True)
    if not cand:
        sys.exit(f"找不到前缀为 {prefix} 的公共镜像")
    return cand[0]["ImageId"]


def custom_image_status(client, region, image_id):
    lst = ecs(client, "DescribeImages", RegionId=region, ImageId=image_id, Status=IMG_STATUS)["Images"]["Image"]
    return lst[0]["Status"] if lst else "NotFound"


def resolve_image(client, region, st, choice):
    """返回 (image_id, 描述, disk_category, disk_size, performance_level)"""
    images = st.get("images", {})
    if choice is None:
        choice = "alpine-cloud" if images.get("alpine-cloud") else ("alpine-iso" if images.get("alpine-iso") else "debian")
    if choice == "debian":
        return pick_public_image(client, region), "Debian 12 公共镜像", "cloud_essd_entry", 20, None
    if choice in images:
        iid = images[choice]["image_id"]
        s = custom_image_status(client, region, iid)
        if s != "Available":
            sys.exit(f"自定义镜像 {choice} ({iid}) 状态 {s}，还不能用。用 import_image.py --status 查看")
        return iid, f"自定义镜像 {choice}", "cloud_essd", images[choice].get("size", 1), "PL0"
    if choice.startswith("m-"):
        return choice, "指定镜像", "cloud_essd", 1, "PL0"
    sys.exit(f"未知镜像选择 {choice}，state 里有: {list(images)}")


def quote(client, region, zone, itype, disk_cat, disk_size, pl):
    extra = {"SystemDisk.Category": disk_cat, "SystemDisk.Size": disk_size}
    if pl:
        extra["SystemDisk.PerformanceLevel"] = pl
    r = ecs(client, "DescribePrice", RegionId=region, ZoneId=zone, ResourceType="instance", InstanceType=itype,
            SpotStrategy="SpotAsPriceGo", PriceUnit="Hour", **extra)
    p = r["PriceInfo"]["Price"]
    parts = {d.get("Resource"): d.get("TradePrice") for d in p.get("DetailInfos", {}).get("DetailInfo", [])}
    return p.get("TradePrice"), parts


# ---------- 资源 ----------
def wait(fn, desc, want, timeout=300, every=5):
    t0 = time.time()
    cur = None
    while time.time() - t0 < timeout:
        cur = fn()
        if cur in want:
            return cur
        time.sleep(every)
    sys.exit(f"等待 {desc} 超时，当前状态 {cur}")


def ensure_vpc(client, region, st, key):
    if not st.get("vpc_id"):
        lst = vpc(client, "DescribeVpcs", RegionId=region, VpcName=NAME)["Vpcs"]["Vpc"]
        if lst:
            st["vpc_id"] = lst[0]["VpcId"]
        else:
            st["vpc_id"] = vpc(client, "CreateVpc", RegionId=region, CidrBlock=VPC_CIDR, VpcName=NAME,
                               ClientToken=f"{NAME}-vpc-{key}")["VpcId"]
            log(f"创建 VPC {st['vpc_id']}")
        save_state(key, st)
    wait(lambda: vpc(client, "DescribeVpcs", RegionId=region, VpcId=st["vpc_id"])["Vpcs"]["Vpc"][0]["Status"], "VPC", {"Available"})
    return st["vpc_id"]


def ensure_vswitch(client, region, st, key, zone):
    key = f"vswitch_{zone}"
    if not st.get(key):
        lst = vpc(client, "DescribeVSwitches", RegionId=region, VpcId=st["vpc_id"], ZoneId=zone)["VSwitches"]["VSwitch"]
        if lst:
            st[key] = lst[0]["VSwitchId"]
        else:
            used = {v["CidrBlock"] for v in vpc(client, "DescribeVSwitches", RegionId=region, VpcId=st["vpc_id"])["VSwitches"]["VSwitch"]}
            n = next(n for n in range(1, 250) if f"172.16.{n}.0/24" not in used)
            st[key] = vpc(client, "CreateVSwitch", RegionId=region, VpcId=st["vpc_id"], ZoneId=zone,
                          CidrBlock=f"172.16.{n}.0/24", VSwitchName=f"{NAME}-{zone}",
                          ClientToken=f"{NAME}-vsw-{zone}-{key}")["VSwitchId"]
            log(f"创建交换机 {st[key]} ({zone})")
        save_state(key, st)
    wait(lambda: vpc(client, "DescribeVSwitches", RegionId=region, VSwitchId=st[key])["VSwitches"]["VSwitch"][0]["Status"], "交换机", {"Available"})
    return st[key]


def ensure_sg(client, region, st, key):
    if not st.get("sg_id"):
        lst = ecs(client, "DescribeSecurityGroups", RegionId=region, VpcId=st["vpc_id"], SecurityGroupName=NAME)["SecurityGroups"]["SecurityGroup"]
        if lst:
            st["sg_id"] = lst[0]["SecurityGroupId"]
        else:
            st["sg_id"] = ecs(client, "CreateSecurityGroup", RegionId=region, VpcId=st["vpc_id"], SecurityGroupName=NAME,
                              Description="cdt keepalive box", ClientToken=f"{NAME}-sg-{key}")["SecurityGroupId"]
            log(f"创建安全组 {st['sg_id']}")
        save_state(key, st)
    for proto, port in (("tcp", "22/22"), ("icmp", "-1/-1")):
        try:
            ecs(client, "AuthorizeSecurityGroup", RegionId=region, SecurityGroupId=st["sg_id"], IpProtocol=proto,
                PortRange=port, SourceCidrIp="0.0.0.0/0", Policy="accept", Priority=1)
        except ApiError as e:
            if "Duplicate" not in err_str(e):
                raise
    return st["sg_id"]


def ensure_keypair(client, region, st, key, pub_path):
    name = st.get("keypair") or f"{NAME}-{key}"
    lst = ecs(client, "DescribeKeyPairs", RegionId=region, KeyPairName=name)["KeyPairs"]["KeyPair"]
    if not lst:
        pub = Path(pub_path).expanduser()
        if not pub.exists():
            sys.exit(f"找不到 SSH 公钥 {pub}，用 --ssh-key 指定")
        ecs(client, "ImportKeyPair", RegionId=region, KeyPairName=name, PublicKeyBody=pub.read_text().strip())
        log(f"导入密钥对 {name} <- {pub}")
    st["keypair"] = name
    save_state(key, st)
    return name


def instance_info(client, region, iid):
    lst = ecs(client, "DescribeInstances", RegionId=region, InstanceIds=[iid])["Instances"]["Instance"]
    return lst[0] if lst else None


def find_instance_by_name(client, region):
    """state 丢了也能接着用：按实例名找已有实例。"""
    lst = ecs(client, "DescribeInstances", RegionId=region, InstanceName=NAME, PageSize=20)["Instances"]["Instance"]
    lst = [i for i in lst if i.get("InstanceName") == NAME]
    return lst[0] if lst else None


def ensure_instance(client, region, st, key, zone, itype, image, disk_cat, disk_size, pl, vsw, sg, keypair):
    if st.get("instance_id") and instance_info(client, region, st["instance_id"]):
        log(f"复用实例 {st['instance_id']}")
        return st["instance_id"]
    extra = {"SystemDisk.Category": disk_cat, "SystemDisk.Size": disk_size}
    if pl:
        extra["SystemDisk.PerformanceLevel"] = pl
    params = dict(RegionId=region, ZoneId=zone, ImageId=image, InstanceType=itype, SecurityGroupId=sg, VSwitchId=vsw,
                  InstanceName=NAME, HostName=NAME, KeyPairName=keypair, InstanceChargeType="PostPaid",
                  SpotStrategy="SpotAsPriceGo", SpotInterruptionBehavior="Stop", InternetMaxBandwidthOut=0,
                  Amount=1, ClientToken=f"{NAME}-ecs-{key}-{int(time.time())}", **extra)
    try:
        r = ecs(client, "RunInstances", **params)
    except ApiError as e:
        if "SpotInterruptionBehavior" in err_str(e):
            log(f"该规格不支持被抢占后停机保留，改为释放模式: {err_str(e)}")
            params["SpotInterruptionBehavior"] = "Terminate"
            r = ecs(client, "RunInstances", **params)
        else:
            raise
    iid = r["InstanceIdSets"]["InstanceIdSet"][0]
    st.update(instance_id=iid, instance_type=itype, zone=zone, image=image, disk=f"{disk_cat}/{disk_size}GiB/{pl}",
              spot_behavior=params["SpotInterruptionBehavior"])
    save_state(key, st)
    log(f"创建实例 {iid}，等待运行...")
    wait(lambda: (instance_info(client, region, iid) or {}).get("Status"), "实例", {"Running"})
    return iid


def ensure_eip(client, region, st, key, iid):
    if not st.get("eip_id"):
        lst = vpc(client, "DescribeEipAddresses", RegionId=region, EipName=NAME)["EipAddresses"]["EipAddress"]
        if lst:
            st["eip_id"], st["eip_ip"] = lst[0]["AllocationId"], lst[0]["IpAddress"]
        else:
            r = vpc(client, "AllocateEipAddress", RegionId=region, InternetChargeType="PayByTraffic", Bandwidth=200,
                    InstanceChargeType="PostPaid", ISP="BGP", Name=NAME, ClientToken=f"{NAME}-eip-{key}")
            st["eip_id"], st["eip_ip"] = r["AllocationId"], r["EipAddress"]
            log(f"创建 EIP {st['eip_ip']} ({st['eip_id']})")
        save_state(key, st)

    def eip():
        return vpc(client, "DescribeEipAddresses", RegionId=region, AllocationId=st["eip_id"])["EipAddresses"]["EipAddress"][0]

    wait(lambda: eip()["Status"], "EIP", {"Available", "InUse"})
    e = eip()
    if e.get("InstanceId") != iid:
        if e.get("InstanceId"):
            sys.exit(f"EIP 已绑定到别的实例 {e['InstanceId']}")
        vpc(client, "AssociateEipAddress", RegionId=region, AllocationId=st["eip_id"], InstanceId=iid, InstanceType="EcsInstance")
        log(f"EIP {st['eip_ip']} 绑定到 {iid}")
        wait(lambda: eip()["Status"], "EIP 绑定", {"InUse"})
    return st["eip_id"], st["eip_ip"]


def ensure_cbwp(client, region, st, key, eip_id, bw, charge):
    if not st.get("cbwp_id"):
        lst = vpc(client, "DescribeCommonBandwidthPackages", RegionId=region, Name=NAME)["CommonBandwidthPackages"]["CommonBandwidthPackage"]
        if lst:
            st["cbwp_id"] = lst[0]["BandwidthPackageId"]
        else:
            try:
                r = vpc(client, "CreateCommonBandwidthPackage", RegionId=region, Bandwidth=bw, InternetChargeType=charge,
                        ISP="BGP", Name=NAME, ClientToken=f"{NAME}-cbwp-{key}")
            except ApiError as e:
                log(f"共享带宽创建失败（先只用 EIP 自身 200Mbps 按流量）: {err_str(e)}")
                st["cbwp_error"] = err_str(e)
                save_state(key, st)
                return None
            st["cbwp_id"] = r["BandwidthPackageId"]
            log(f"创建共享带宽 {st['cbwp_id']} {bw}Mbps {charge}")
        st.pop("cbwp_error", None)
        save_state(key, st)
    pkg = vpc(client, "DescribeCommonBandwidthPackages", RegionId=region, BandwidthPackageId=st["cbwp_id"])["CommonBandwidthPackages"]["CommonBandwidthPackage"][0]
    ips = {p["AllocationId"] for p in pkg.get("PublicIpAddresses", {}).get("PublicIpAddresse", [])}
    if eip_id not in ips:
        vpc(client, "AddCommonBandwidthPackageIp", RegionId=region, BandwidthPackageId=st["cbwp_id"], IpInstanceId=eip_id)
        log(f"EIP 加入共享带宽 {st['cbwp_id']}")
    return st["cbwp_id"]


# ---------- 主流程 ----------
def preflight(client, region, quiet=False):
    checks = {
        "ecs:DescribeInstances": lambda: ecs(client, "DescribeInstances", RegionId=region, PageSize=1),
        "vpc:CreateVpc(DryRun)": lambda: vpc(client, "CreateVpc", RegionId=region, CidrBlock=VPC_CIDR, DryRun="true"),
        "vpc:AllocateEipAddress(DryRun)": lambda: vpc(client, "AllocateEipAddress", RegionId=region, InternetChargeType="PayByTraffic", Bandwidth=200, DryRun="true"),
        "vpc:CreateCommonBandwidthPackage(DryRun)": lambda: vpc(client, "CreateCommonBandwidthPackage", RegionId=region, Bandwidth=1000, InternetChargeType="PayByDominantTraffic", DryRun="true"),
        "cdt:GetCdtServiceStatus": lambda: call(client, "cdt.aliyuncs.com", "2021-08-13", "GetCdtServiceStatus"),
        "cdt:ListCdtInternetTraffic": lambda: call(client, "cdt.aliyuncs.com", "2021-08-13", "ListCdtInternetTraffic"),
    }
    missing = []
    for name, fn in checks.items():
        try:
            fn()
            if not quiet:
                print(f"   ok   {name}")
        except ApiError as e:
            s = err_str(e)
            if s.startswith("DryRunOperation"):
                if not quiet:
                    print(f"   ok   {name}")
            else:
                if not quiet:
                    print(f"   MISS {name}: {s}")
                missing.append(f"{name}: {s}" if quiet else name)
    return missing


def ensure_cdt(client):
    try:
        st = call(client, "cdt.aliyuncs.com", "2021-08-13", "GetCdtServiceStatus")
        if st.get("Enabled") is False:
            call(client, "cdt.aliyuncs.com", "2021-08-13", "OpenCdtService")
            log("已开通 CDT")
        else:
            log(f"CDT 状态: {st}")
    except ApiError as e:
        log(f"CDT 状态检查/开通失败（不影响建机）: {err_str(e)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="只看方案、价格、权限，不创建")
    ap.add_argument("--account", help="只处理这个账号别名")
    ap.add_argument("--image", default=None, help="alpine-cloud | alpine-iso | debian | m-xxxx（默认优先已导入的 alpine-cloud）")
    ap.add_argument("--type", default=None, help=f"实例规格，默认 {DEFAULT_TYPE}")
    ap.add_argument("--zone", default=None, help="可用区，默认选该规格抢占价最低的")
    ap.add_argument("--ssh-key", default="~/.ssh/id_rsa.pub", help="导入的 SSH 公钥")
    ap.add_argument("--cbwp-bandwidth", type=int, default=2000, help="共享带宽峰值 Mbps")
    ap.add_argument("--cbwp-charge", default="PayByDominantTraffic", help="共享带宽计费方式（帖子用按主流量）")
    ap.add_argument("--no-cbwp", action="store_true", help="不建共享带宽，只用 EIP 自身 200Mbps")
    args = ap.parse_args()

    defaults, accounts = load_config()
    for key, acc in accounts.items():
        if args.account and key != args.account:
            continue
        if not acc.get("access_key_id"):
            print(f"跳过 账号 {key}: 未填 AK")
            continue
        client, region = make_client(acc, defaults)
        itype = args.type or defaults.get("instance_type") or DEFAULT_TYPE
        st = load_state(acc)
        print(f"\n#### 账号 {key} ({acc.get('name')}) region={region}")
        if not st.get("instance_id"):
            found = find_instance_by_name(client, region)
            if found:
                st["instance_id"], st["zone"] = found["InstanceId"], found.get("ZoneId")
                save_state(key, st)
                print(f"按名称找到已有实例 {found['InstanceId']}，复用")

        print("权限预检:")
        missing = preflight(client, region)
        eip_ok = not any(m.startswith("vpc:AllocateEipAddress") for m in missing)

        image, image_desc, disk_cat, disk_size, pl = resolve_image(client, region, st, args.image)
        zone = st.get("zone") if st.get("instance_id") else None
        zone, spot, orig = pick_zone(client, region, itype, zone or args.zone or defaults.get("zone_id"))
        total, parts = quote(client, region, zone, itype, disk_cat, disk_size, pl)
        print(f"方案: {itype} @ {zone}  {image_desc} {image}  系统盘 {disk_cat} {disk_size}GiB {pl or ''}")
        print(f"价格: 抢占价 {spot} 元/时 (原价 {orig})，合计 {total} 元/时 {parts}")
        print(f"      约 {float(total) * 24 * 30:.2f} 元/月（实例+系统盘；公网流量走 CDT，每月前 200G 免费）")
        if st:
            print(f"已有状态: { {k: v for k, v in st.items() if k != 'images'} }")
        if args.plan:
            print("--plan 模式，不创建。" + (f" 缺权限: {missing}" if missing else " 权限齐全。"))
            continue
        if any(m.startswith("vpc:CreateVpc") for m in missing):
            print("缺少 VPC 权限，无法继续")
            continue

        ensure_cdt(client)
        ensure_vpc(client, region, st, key)
        vsw = ensure_vswitch(client, region, st, key, zone)
        sg = ensure_sg(client, region, st, key)
        keypair = ensure_keypair(client, region, st, key, args.ssh_key)
        iid = ensure_instance(client, region, st, key, zone, itype, image, disk_cat, disk_size, pl, vsw, sg, keypair)
        info = instance_info(client, region, iid) or {}
        priv = (info.get("VpcAttributes", {}).get("PrivateIpAddress", {}).get("IpAddress") or ["?"])[0]
        print(f"\n实例: {iid}  状态 {info.get('Status')}  内网 {priv}")

        if not eip_ok:
            print("EIP/共享带宽权限缺失，这一步先跳过；补权限后重跑 provision.py 即可补齐。")
            continue
        eip_id, eip_ip = ensure_eip(client, region, st, key, iid)
        if not args.no_cbwp:
            ensure_cbwp(client, region, st, key, eip_id, args.cbwp_bandwidth, args.cbwp_charge)
        print(f"完成: 公网 IP {eip_ip}  登录: ssh alpine@{eip_ip} 或 ssh root@{eip_ip}（看镜像）  状态文件 state/{key}.json")


if __name__ == "__main__":
    main()
