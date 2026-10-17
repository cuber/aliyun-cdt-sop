#!/usr/bin/env python3
"""一条命令看全局状态并给出下一步，供 AI 判断进度用。

  python status.py          # 人类可读
  python status.py --json   # 机器可读

不打印任何密钥；EIP 只给出"是否已绑定"，IP 本身在 state/<name>.json。
"""
import argparse
import json

import provision as P
from aliyun_common import CONFIG, ROOT, ApiError, call, ecs, err_str, load_config, load_state, make_client

NEXT = {
    "config": "步骤 1：让用户 cp config.yaml.template config.yaml && chmod 600 config.yaml，填 AK/SK",
    "creds": "步骤 2：凭证无效，让用户检查 config.yaml 里的 AK/SK",
    "perm": "步骤 3：让用户在 RAM 控制台补权限（README 第 2 步）",
    "image": "步骤 4：导入镜像（AGENTS.md「导入镜像」）",
    "image_wait": "步骤 4：镜像还在导入，等 30 秒再查 import_image.py --status",
    "provision": "步骤 5：.venv/bin/python provision.py --image alpine-cloud --no-cbwp",
    "eip": "步骤 5：实例已有但没绑 EIP，重跑 provision.py --image alpine-cloud --no-cbwp",
    "cdt": "CDT 流量读不到，检查 AliyunCDTFullAccess 是否已挂",
    "keepalive": "实例状态与保活期望不符，跑 .venv/bin/python keepalive.py",
    "done": "步骤 6/7/8：验证实例，然后问用户保活放哪台机器、是否收窄权限",
}


def account_status(key, acc, defaults):
    a = {"account": key}
    client, region = make_client(acc, defaults)
    try:
        ident = call(client, "sts.aliyuncs.com", "2015-04-01", "GetCallerIdentity")
        a["credentials"] = "ok"
        a["identity_type"] = ident.get("IdentityType")
    except ApiError as e:
        a["credentials"] = err_str(e)
        a["next"] = NEXT["creds"]
        return a

    missing = P.preflight(client, region, quiet=True)
    a["missing_permissions"] = [m for m in missing if "CreateCommonBandwidthPackage" not in m]
    a["shared_bandwidth_blocked"] = any("CreateCommonBandwidthPackage" in m for m in missing)

    st = load_state(acc)
    img = st.get("images", {}).get("alpine-cloud", {}).get("image_id")
    status = P.custom_image_status(client, region, img) if img else "NotFound"
    if status == "NotFound":
        try:
            lst = ecs(client, "DescribeImages", RegionId=region, ImageOwnerAlias="self", ImageName="alpine-cloud", Status=P.IMG_STATUS)["Images"]["Image"]
            lst = [i for i in lst if i.get("ImageName") == "alpine-cloud"]
            if lst:
                status = lst[0]["Status"]
        except ApiError:
            pass
    a["image_alpine_cloud"] = status

    iid = st.get("instance_id")
    info = (P.instance_info(client, region, iid) if iid else None) or P.find_instance_by_name(client, region)
    a["instance"] = None
    if info:
        a["instance"] = {"status": info.get("Status"), "type": info.get("InstanceType"), "spot": info.get("SpotStrategy"),
                         "eip_bound": bool(info.get("EipAddress", {}).get("IpAddress"))}

    try:
        r = call(client, "cdt.aliyuncs.com", "2021-08-13", "ListCdtInternetTraffic")
        a["cdt_used_gb"] = round(sum(d.get("Traffic", 0) for d in r.get("TrafficDetails", [])) / 2 ** 30, 2)
    except ApiError as e:
        a["cdt_used_gb"] = None
        a["cdt_error"] = err_str(e)
    thr = defaults.get("traffic_threshold_gb") or 180
    a["threshold_gb"] = thr

    if a["missing_permissions"]:
        a["next"] = NEXT["perm"] + "：" + "；".join(a["missing_permissions"])
    elif status in ("NotFound", "CreateFailed", "UnAvailable"):
        a["next"] = NEXT["image"]
    elif status != "Available":
        a["next"] = NEXT["image_wait"]
    elif not a["instance"]:
        a["next"] = NEXT["provision"]
    elif not a["instance"]["eip_bound"]:
        a["next"] = NEXT["eip"]
    elif a["cdt_used_gb"] is None:
        a["next"] = NEXT["cdt"]
    else:
        want = "Running" if a["cdt_used_gb"] < thr else "Stopped"
        a["keepalive_expect"] = want
        a["next"] = NEXT["done"] if a["instance"]["status"] == want else NEXT["keepalive"]
    return a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = {"venv": (ROOT / ".venv" / "bin" / "python").exists(), "config": CONFIG.exists(), "accounts": []}
    if report["config"]:
        defaults, accounts = load_config()
        for key, acc in accounts.items():
            if acc.get("access_key_id"):
                report["accounts"].append(account_status(key, acc, defaults))
            else:
                report["accounts"].append({"account": key, "credentials": "empty", "next": NEXT["config"]})
    else:
        report["next"] = NEXT["config"]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"venv: {'ok' if report['venv'] else '缺'}   config.yaml: {'ok' if report['config'] else '缺'}")
    if not report["config"]:
        print("下一步:", report["next"])
        return
    for a in report["accounts"]:
        print(f"\n== 账号 {a['account']}")
        print(f"   凭证: {a.get('credentials')}  {a.get('identity_type', '')}")
        if "missing_permissions" in a:
            print(f"   缺权限: {a['missing_permissions'] or '无'}   共享带宽被风控: {a.get('shared_bandwidth_blocked')}")
            print(f"   镜像 alpine-cloud: {a.get('image_alpine_cloud')}")
            inst = a.get("instance")
            print(f"   实例: {inst if inst else '无'}")
            print(f"   CDT 本月已用: {a.get('cdt_used_gb')} GB / 阈值 {a.get('threshold_gb')}   保活期望: {a.get('keepalive_expect', '-')}")
        print(f"   下一步: {a.get('next')}")


if __name__ == "__main__":
    main()
