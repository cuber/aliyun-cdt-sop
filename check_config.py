#!/usr/bin/env python3
"""校验 config.yaml。

对每个账号:
  1. 调 STS GetCallerIdentity，确认 AK/SK 有效；配置里填了 uid 的话再核对归属（防止填串账号）
  2. 找实例（state 文件，或按实例名 cdt-hk）并显示状态；还没建机时只是提示
  3. 调 CDT ListCdtInternetTraffic，确认有权限读取本月流量

只打印脱敏后的 AccessKey ID 前 4 位，绝不输出 Secret。
"""
import json
import stat
import sys
from pathlib import Path

from aliyunsdkcore.acs_exception.exceptions import ClientException, ServerException
from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest

from aliyun_common import CONFIG, load_config, load_state


def mask(value: str) -> str:
    value = value or ""
    return value[:4] + "*" * max(len(value) - 4, 0) if value else "(空)"


def call(client, domain, version, action, **params):
    req = CommonRequest()
    req.set_domain(domain)
    req.set_version(version)
    req.set_action_name(action)
    req.set_method("POST")
    req.set_accept_format("json")
    req.set_protocol_type("https")
    for key, value in params.items():
        req.add_query_param(key, value)
    return json.loads(client.do_action_with_exception(req))


def warn_permissions(path: Path):
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        print(f"[警告] {path.name} 权限为 {oct(mode)}，建议执行: chmod 600 {path.name}")


def check_account(acc: dict, defaults: dict) -> bool:
    ok = True
    name = acc["key"]
    uid = acc.get("uid")
    ak = acc.get("access_key_id") or ""
    sk = acc.get("access_key_secret") or ""
    region = acc.get("region_id") or defaults.get("region_id") or "cn-hongkong"
    print(f"\n== 账号 {name}  region={region}  ak={mask(ak)}" + (f"  uid={uid}" if uid else ""))

    if not ak or not sk:
        print("   [跳过] access_key_id / access_key_secret 未填写")
        return False

    client = AcsClient(ak, sk, region)

    # 1. 身份与 UID 一致性
    try:
        ident = call(client, "sts.aliyuncs.com", "2015-04-01", "GetCallerIdentity")
    except (ClientException, ServerException) as e:
        print(f"   [失败] 凭证无效: {e.get_error_code()} {e.get_error_msg()}")
        return False
    real_uid = str(ident.get("AccountId"))
    if uid and real_uid != uid:
        print(f"   [错误] 这对 AK 实际属于 UID {real_uid}，与配置的 {uid} 不一致，可能填串了")
        return False
    print(f"   [通过] 身份: {ident.get('IdentityType')}  {ident.get('Arn')}")
    if ident.get("IdentityType") == "Account":
        print("   [警告] 这是主账号 AK，建议换成 RAM 子用户 AK，并只授予 ram-policy.json 中的权限")

    # 2. 实例可见性：config 里的 instances（可选）> state 文件 > 按实例名 cdt-hk 找
    ids = [i.get("instance_id") for i in (acc.get("instances") or []) if i and i.get("instance_id")]
    if not ids and load_state(acc).get("instance_id"):
        ids = [load_state(acc)["instance_id"]]
    if not ids:
        try:
            found = call(client, "ecs.aliyuncs.com", "2014-05-26", "DescribeInstances", RegionId=region, InstanceName="cdt-hk", PageSize=20)
            ids = [i["InstanceId"] for i in found.get("Instances", {}).get("Instance", []) if i.get("InstanceName") == "cdt-hk"]
        except (ClientException, ServerException):
            ids = []
    if not ids:
        print("   [提示] 还没有实例（provision.py 建机后这里会自动找到）")
    else:
        try:
            resp = call(client, "ecs.aliyuncs.com", "2014-05-26", "DescribeInstances",
                        RegionId=region, InstanceIds=json.dumps(ids))
            found = {i["InstanceId"]: i for i in resp.get("Instances", {}).get("Instance", [])}
            for iid in ids:
                inst = found.get(iid)
                if inst:
                    print(f"   [通过] 实例 {iid}: status={inst.get('Status')} "
                          f"type={inst.get('InstanceType')} spot={inst.get('SpotStrategy')}")
                else:
                    print(f"   [错误] 实例 {iid} 在 {region} 下找不到（地域错了或不属于这个账号）")
                    ok = False
        except (ClientException, ServerException) as e:
            print(f"   [失败] ECS DescribeInstances: {e.get_error_code()} {e.get_error_msg()}")
            ok = False

    # 3. CDT 流量权限
    try:
        resp = call(client, "cdt.aliyuncs.com", "2021-08-13", "ListCdtInternetTraffic")
        total = sum(d.get("Traffic", 0) for d in resp.get("TrafficDetails", []))
        print(f"   [通过] CDT 本月公网流量: {total / 1024 ** 3:.2f} GB")
    except (ClientException, ServerException) as e:
        print(f"   [失败] CDT ListCdtInternetTraffic: {e.get_error_code()} {e.get_error_msg()}")
        ok = False

    return ok


def main():
    if not CONFIG.exists():
        sys.exit("找不到 config.yaml，先执行: cp config.yaml.template config.yaml")
    warn_permissions(CONFIG)
    defaults, accounts = load_config()
    if not accounts:
        sys.exit("accounts 为空，请至少配置一个账号")

    all_ok = True
    for acc in accounts.values():
        all_ok &= check_account(acc, defaults)

    print("\n全部通过" if all_ok else "\n存在问题，见上方输出")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
