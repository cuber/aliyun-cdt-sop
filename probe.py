#!/usr/bin/env python3
"""探测账号在香港地域的现状与可用资源，结果写入 state/probe_<name>.json（不含任何密钥）。"""
import datetime
import json
import sys

from aliyun_common import ApiError, STATE_DIR, call, ecs, err_str, load_config, make_client, vpc

defaults, accounts = load_config()
STATE_DIR.mkdir(exist_ok=True)


def attempt(label, fn, out):
    try:
        res = fn()
        out[label] = res
        return res
    except ApiError as e:
        out[label] = {"_error": err_str(e)}
        print(f"  [x] {label}: {err_str(e)}")
        return None


for key, acc in accounts.items():
    if not acc.get("access_key_id"):
        continue
    client, region = make_client(acc, defaults)
    out = {"key": key, "region": region}
    print(f"\n#### 账号 {key} ({acc.get('name')}) region={region}")

    r = attempt("balance", lambda: call(client, "business.aliyuncs.com", "2017-12-14", "QueryAccountBalance"), out)
    if r:
        d = r.get("Data", {})
        print(f"  balance: {d.get('AvailableAmount')} {d.get('Currency')}  cash={d.get('AvailableCashAmount')}")

    r = attempt("cdt_status", lambda: call(client, "cdt.aliyuncs.com", "2021-08-13", "GetCdtServiceStatus"), out)
    if r:
        print(f"  cdt: {r}")

    r = attempt("image_import_role", lambda: call(client, "ram.aliyuncs.com", "2015-05-01", "GetRole", RoleName="AliyunECSImageImportDefaultRole"), out)
    if r:
        print(f"  image import role: exists")

    r = attempt("zones", lambda: ecs(client, "DescribeZones", RegionId=region), out)
    zones = [z["ZoneId"] for z in (r or {}).get("Zones", {}).get("Zone", [])]
    print(f"  zones: {zones}")

    r = attempt("vpcs", lambda: vpc(client, "DescribeVpcs", RegionId=region, PageSize=50), out)
    if r:
        for v in r.get("Vpcs", {}).get("Vpc", []):
            print(f"  vpc: {v['VpcId']} {v.get('VpcName')} {v.get('CidrBlock')} default={v.get('IsDefault')}")
    r = attempt("vswitches", lambda: vpc(client, "DescribeVSwitches", RegionId=region, PageSize=50), out)
    if r:
        for v in r.get("VSwitches", {}).get("VSwitch", []):
            print(f"  vswitch: {v['VSwitchId']} {v.get('VSwitchName')} {v.get('ZoneId')} {v.get('CidrBlock')} vpc={v.get('VpcId')}")
    r = attempt("security_groups", lambda: ecs(client, "DescribeSecurityGroups", RegionId=region, PageSize=50), out)
    if r:
        for g in r.get("SecurityGroups", {}).get("SecurityGroup", []):
            print(f"  sg: {g['SecurityGroupId']} {g.get('SecurityGroupName')} vpc={g.get('VpcId')}")
    r = attempt("keypairs", lambda: ecs(client, "DescribeKeyPairs", RegionId=region, PageSize=50), out)
    if r:
        print(f"  keypairs: {[k['KeyPairName'] for k in r.get('KeyPairs', {}).get('KeyPair', [])]}")
    r = attempt("instances", lambda: ecs(client, "DescribeInstances", RegionId=region, PageSize=50), out)
    if r:
        for i in r.get("Instances", {}).get("Instance", []):
            print(f"  instance: {i['InstanceId']} {i.get('InstanceName')} {i.get('InstanceType')} {i.get('Status')} spot={i.get('SpotStrategy')}")
    r = attempt("eips", lambda: vpc(client, "DescribeEipAddresses", RegionId=region, PageSize=50), out)
    if r:
        for e in r.get("EipAddresses", {}).get("EipAddress", []):
            print(f"  eip: {e['AllocationId']} {e.get('IpAddress')} {e.get('Status')} charge={e.get('InternetChargeType')} bw={e.get('Bandwidth')} cbwp={e.get('BandwidthPackageId')}")
    r = attempt("cbwps", lambda: vpc(client, "DescribeCommonBandwidthPackages", RegionId=region, PageSize=50), out)
    if r:
        for c in r.get("CommonBandwidthPackages", {}).get("CommonBandwidthPackage", []):
            print(f"  cbwp: {c['BandwidthPackageId']} {c.get('Name')} bw={c.get('Bandwidth')} charge={c.get('InternetChargeType')} status={c.get('Status')}")

    # 公共镜像（Debian / Ubuntu）与自定义镜像
    imgs = []
    page = 1
    while True:
        r = attempt(f"images_p{page}", lambda: ecs(client, "DescribeImages", RegionId=region, ImageOwnerAlias="system", OSType="linux", PageSize=100, PageNumber=page), out)
        if not r:
            break
        batch = r.get("Images", {}).get("Image", [])
        imgs += batch
        if len(batch) < 100:
            break
        page += 1
    out["public_images"] = [{"ImageId": i["ImageId"], "OSName": i.get("OSNameEn") or i.get("OSName"), "Platform": i.get("Platform"), "Size": i.get("Size"), "Arch": i.get("Architecture")} for i in imgs]
    for i in imgs:
        if i.get("Platform") in ("Debian", "Ubuntu") and i.get("Architecture") == "x86_64":
            print(f"  image: {i['ImageId']} {i.get('OSNameEn')} size={i.get('Size')}G")
    r = attempt("custom_images", lambda: ecs(client, "DescribeImages", RegionId=region, ImageOwnerAlias="self", PageSize=100), out)
    if r:
        print(f"  custom images: {[(i['ImageId'], i.get('ImageName')) for i in r.get('Images', {}).get('Image', [])]}")

    # 可用的抢占式实例规格
    r = attempt("avail_spot", lambda: ecs(client, "DescribeAvailableResource", RegionId=region, DestinationResource="InstanceType",
                                          InstanceChargeType="PostPaid", SpotStrategy="SpotAsPriceGo", NetworkCategory="vpc", IoOptimized="optimized"), out)
    avail = {}  # type -> [zones]
    if r:
        for z in r.get("AvailableZones", {}).get("AvailableZone", []):
            for ar in z.get("AvailableResources", {}).get("AvailableResource", []):
                for s in ar.get("SupportedResources", {}).get("SupportedResource", []):
                    if s.get("Status") == "Available":
                        avail.setdefault(s["Value"], []).append(z["ZoneId"])
    print(f"  spot-capable types: {len(avail)}")

    # 规格参数（<=2 vCPU）
    types = {}
    token = None
    while True:
        r = attempt("instance_types", lambda: ecs(client, "DescribeInstanceTypes", MaximumCpuCoreCount=2, MaxResults=100, NextToken=token), out)
        if not r:
            break
        for t in r.get("InstanceTypes", {}).get("InstanceType", []):
            types[t["InstanceTypeId"]] = {"cpu": t.get("CpuCoreCount"), "mem": t.get("MemorySize"), "family": t.get("InstanceTypeFamily"),
                                          "bw_mbps": t.get("InstanceBandwidthRx"), "disks": t.get("DiskQuantity"), "baseline": t.get("BaselineCredit")}
        token = r.get("NextToken")
        if not token:
            break
    cands = {t: z for t, z in avail.items() if t in types and (types[t]["mem"] or 99) <= 4}
    print(f"  candidates (<=2c, <=4G): {len(cands)}")

    # 抢占价格
    start = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    prices = []
    for t, zs in sorted(cands.items()):
        for z in zs:
            try:
                r = ecs(client, "DescribeSpotPriceHistory", RegionId=region, ZoneId=z, InstanceType=t, NetworkType="vpc", IoOptimized="optimized", StartTime=start)
                hist = r.get("SpotPrices", {}).get("SpotPriceType", [])
                if hist:
                    h = hist[-1]
                    prices.append({"type": t, "zone": z, "spot": h.get("SpotPrice"), "origin": h.get("OriginPrice"), **types[t]})
            except ApiError as e:
                prices.append({"type": t, "zone": z, "_error": err_str(e)})
    out["spot_prices"] = prices
    ok = [p for p in prices if "spot" in p]
    ok.sort(key=lambda p: p["spot"])
    print("  cheapest spot (CNY/hour, ~hours*730=month):")
    for p in ok[:25]:
        print(f"    {p['type']:<24} {p['zone']}  spot={p['spot']:.4f}  orig={p['origin']:.4f}  {p['cpu']}c{p['mem']}G  mo≈{p['spot']*730:.2f}")

    (STATE_DIR / f"probe_{key}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"  -> state/probe_{key}.json")
